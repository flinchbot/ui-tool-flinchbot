"""
Local, append-only history files in data/ (never committed, never sent anywhere):

  data/audit.jsonl       every approved or denied change: who, when, which
                         connection, what (secrets masked), the console's answer,
                         and an undo snapshot where possible (see rollback.py)
  data/speedtests.jsonl  speed tests this app ran (the console keeps its own too)

Each file rotates to <name>-<timestamp>.jsonl at 5 MB; read() reads the rotated files too,
so nothing drops out of the app (change log, Undo, spending, speed tests) when that happens.
"""

import getpass
import json
import os
import re
import socket
import threading
import uuid
from datetime import datetime

import config_store as cfg
import rollback
from unifi_client import UniFiError

AUDIT, SPEED = "audit.jsonl", "speedtests.jsonl"
MAX_BYTES = 5_000_000
_lock = threading.RLock()


def _path(name):
    return os.path.join(cfg.DATA_DIR, name)


def append(name, record):
    line = json.dumps(record, default=str, ensure_ascii=False)
    with _lock:
        os.makedirs(cfg.DATA_DIR, exist_ok=True)
        p = _path(name)
        if os.path.exists(p) and os.path.getsize(p) > MAX_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest, n = p.replace(".jsonl", f"-{stamp}.jsonl"), 1
            while os.path.exists(dest):            # two rotations in one second must not overwrite
                n += 1
                dest = p.replace(".jsonl", f"-{stamp}-{n:03d}.jsonl")
            os.replace(p, dest)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    return record


def _segments(name):
    """The rotated files of a log, oldest first, then the current one. Rotation only renames
    a file, so together they are the whole history (audit and Undo, spending, speed tests)."""
    base = name[:-len(".jsonl")]
    pat = re.compile(re.escape(base) + r"-(\d{8}-\d{6})(?:-(\d{3}))?\.jsonl$")
    try:
        found = [(m.group(1), int(m.group(2) or 1), f) for f in os.listdir(cfg.DATA_DIR) if (m := pat.match(f))]
    except OSError:
        found = []
    return [os.path.join(cfg.DATA_DIR, f) for _, _, f in sorted(found)] + [_path(name)]


def read(name):
    """All records, oldest first, from the rotated files too. Bad lines are skipped."""
    out = []
    with _lock:
        for p in _segments(name):
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
    return out


def _who():
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return user, socket.gethostname()


def same_target(rec, profile):
    """Is `profile` still the console and site the change `rec` was made on? The connection id
    alone isn't enough: editing a saved connection keeps its id but can point it at another
    console (host, port) or site, and Undo must never change that other network."""
    if not profile or profile.get("id") != rec.get("connection_id"):
        return False
    if str(profile.get("host") or "").strip().lower() != str(rec.get("host") or "").strip().lower():
        return False
    if (profile.get("site") or "default") != (rec.get("site") or "default"):
        return False
    # Records written before 2.17 have no port: host and site are what can be checked then.
    return rec.get("port") in (None, "") or str(profile.get("port") or "") == str(rec.get("port"))


class ChangeLog:
    """The audit log plus undo. get_client/get_profile are the app's live accessors."""

    def __init__(self, get_client, get_profile):
        self._get_client = get_client
        self._get_profile = get_profile
        self._undo_lock = threading.Lock()
        self._undoing = set()       # record ids an Undo is running for right now
        self._undone = {}           # record id -> detail: undone in this run even if the log couldn't say so

    # used by the agent around an approved change ---------------------------
    def prepare(self, tool, inp, get_client=None):
        """-> (snapshot or None, note or None). Never raises: a failed snapshot only means no undo.
        get_client: the client of the connection the change is for (default: the active one)."""
        if tool not in rollback.ROLLBACK_TOOLS:
            return None, None
        try:
            return rollback.prepare((get_client or self._get_client)(), tool, inp), None
        except Exception as e:
            return None, f"Undo not available: couldn't read the current settings first ({e})."

    def record(self, tool, inp, label, decision, result=None, prep=None, via="chat", undoes=None, note=None,
               target=None, result_obj=None, profile=None):
        """profile: the connection the change was made on (default: the active one). Pass it
        whenever the change started before now, so a switch in between can't mislabel it."""
        p = profile if profile and profile.get("id") else (self._get_profile() or {})
        user, pc = _who()
        rec = {"id": uuid.uuid4().hex[:12], "at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "user": user, "pc": pc, "connection_id": p.get("id"), "connection": p.get("name"),
               "host": p.get("host"), "port": p.get("port"), "site": p.get("site") or "default",
               "tool": tool, "label": label, "target": target, "input": rollback.mask(inp or {}),
               "decision": decision, "via": via}
        if result is not None:
            rec["ok"] = not result.get("is_error")
            rec["result"] = str(result.get("content", ""))[:500]
        if undoes:
            rec["undoes"] = undoes
        if note:
            rec["note"] = note
        prep = rollback.finish(prep, result_obj) if decision == "approved" and rec.get("ok") else None
        if prep:
            try:
                rec["rollback"] = rollback.store(prep, rec["id"])
                rec["rollback"]["describe"] = rollback.describe(rec["rollback"])
            except Exception as e:      # e.g. the secret store refused it
                rec["note"] = f"Undo not available: couldn't store the previous settings safely ({e})."
        return append(AUDIT, rec)

    # read / undo -----------------------------------------------------------
    def list(self, limit=300, connection_id=None):
        recs = read(AUDIT)
        undone = {r["undoes"]: r["id"] for r in recs if r.get("undoes") and r.get("ok")}
        active = self._get_profile() or {}
        out = []
        for r in reversed(recs):
            if connection_id and r.get("connection_id") != connection_id:
                continue
            r = dict(r)
            r["undone_by"] = undone.get(r["id"]) or ("unlogged" if r["id"] in self._undone else None)
            r["can_undo"] = bool(r.get("rollback") and not r["undone_by"] and r.get("ok"))
            r["undo_blocked"] = self._blocked(r, active) if r["can_undo"] else None
            r["undo_here"] = r["can_undo"] and not r["undo_blocked"]
            out.append(r)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _blocked(r, profile):
        """-> why Undo can't run on this connection, or None."""
        if not profile or profile.get("id") != r.get("connection_id"):
            return (f"That change was made on “{r.get('connection')}”. Switch to that connection first, "
                    f"then undo it.")
        if not same_target(r, profile):
            return (f"That change was made on {r.get('host')} (site {r.get('site') or 'default'}), but "
                    f"“{profile.get('name')}” now points to {profile.get('host')} (site "
                    f"{profile.get('site') or 'default'}). Undo only runs on the console and site the change "
                    f"was made on.")
        return None

    def get(self, rid):
        return next((r for r in self.list(limit=10 ** 9) if r["id"] == rid), None)

    def undo(self, rid, force=False):
        """One Undo at a time per change: two clicks (two tabs, a double click) must not
        recreate a deleted Wi-Fi network twice."""
        with self._undo_lock:
            if rid in self._undoing:
                raise UniFiError("That change is being undone right now.", "rollback")
            self._undoing.add(rid)
        try:
            return self._undo(rid, force)
        finally:
            with self._undo_lock:
                self._undoing.discard(rid)

    def _record_quietly(self, *args, **kw):
        """Log an Undo. The undo has already happened (or failed) on the console by now, so a
        change log that can't be written must not change what the user is told. -> error or None."""
        try:
            self.record(*args, **kw)
            return None
        except Exception as e:
            print(f"  change log failed: {type(e).__name__}: {e}")
            return f"The change log couldn't be written ({e}), so this undo isn't in it."

    def _undo(self, rid, force):
        r = self.get(rid)
        if not r:
            raise UniFiError("That change isn't in the log.", "rollback")
        if r.get("undone_by"):
            raise UniFiError("That change was already undone.", "rollback")
        if not r.get("can_undo"):
            raise UniFiError("That change can't be undone automatically.", "rollback")
        if not r["undo_here"]:
            raise UniFiError(r.get("undo_blocked") or "That change can't be undone on this connection.", "rollback")
        client = self._get_client()
        cp = getattr(client, "profile", None)
        why = self._blocked(r, cp) if cp and cp.get("id") else None    # switched or edited just now
        if why:
            raise UniFiError(why, "rollback")
        where = {"id": r.get("connection_id"), "name": r.get("connection"), "host": r.get("host"),
                 "port": r.get("port"), "site": r.get("site")}
        label = f"Undo: {r['label']}"
        try:
            res = rollback.undo(client, r["rollback"], r["id"], force=force)
        except UniFiError as e:
            self._record_quietly(r["tool"], r.get("input"), label, "approved", {"content": str(e), "is_error": True},
                                 via="undo", undoes=r["id"], target=r.get("target"), profile=where)
            raise
        if res.get("conflict"):
            return res
        self._undone[r["id"]] = res["detail"]               # can't be undone twice, whatever the log says
        log_error = self._record_quietly(r["tool"], r.get("input"), label, "approved",
                                         {"content": res["detail"], "is_error": False}, via="undo",
                                         undoes=r["id"], target=r.get("target"), profile=where)
        try:
            rollback.forget(r["id"])
        except Exception as e:
            print(f"  couldn't remove the saved undo secrets: {e}")
        return dict(res, log_error=log_error) if log_error else res


# ── speed tests this app ran ─────────────────────────────────────────────────
def record_speedtest(connection_id, point, via):
    return append(SPEED, dict(point, connection_id=connection_id, via=via))


def speedtests(connection_id, since_ms=0):
    return [r for r in read(SPEED) if r.get("connection_id") == connection_id and (r.get("at") or 0) >= since_ms]
