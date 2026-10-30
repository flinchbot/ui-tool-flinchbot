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


class ChangeLog:
    """The audit log plus undo. get_client/get_profile are the app's live accessors."""

    def __init__(self, get_client, get_profile):
        self._get_client = get_client
        self._get_profile = get_profile

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
               "host": p.get("host"), "site": p.get("site", "default"),
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
        active = (self._get_profile() or {}).get("id")
        out = []
        for r in reversed(recs):
            if connection_id and r.get("connection_id") != connection_id:
                continue
            r = dict(r)
            r["undone_by"] = undone.get(r["id"])
            r["can_undo"] = bool(r.get("rollback") and not r["undone_by"] and r.get("ok"))
            r["undo_here"] = r["can_undo"] and r.get("connection_id") == active
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def get(self, rid):
        return next((r for r in self.list(limit=10 ** 9) if r["id"] == rid), None)

    def undo(self, rid, force=False):
        r = self.get(rid)
        if not r:
            raise UniFiError("That change isn't in the log.", "rollback")
        if r.get("undone_by"):
            raise UniFiError("That change was already undone.", "rollback")
        if not r.get("can_undo"):
            raise UniFiError("That change can't be undone automatically.", "rollback")
        if not r["undo_here"]:
            raise UniFiError(f"That change was made on “{r.get('connection')}”. Switch to that connection "
                             f"first, then undo it.", "rollback")
        client = self._get_client()
        cid = (getattr(client, "profile", None) or {}).get("id")
        if cid and cid != r.get("connection_id"):          # the connection was switched just now
            raise UniFiError(f"That change was made on “{r.get('connection')}”. Switch to that connection "
                             f"first, then undo it.", "rollback")
        where = {"id": r.get("connection_id"), "name": r.get("connection"), "host": r.get("host"),
                 "site": r.get("site")}
        try:
            res = rollback.undo(client, r["rollback"], r["id"], force=force)
        except UniFiError as e:
            self.record(r["tool"], r.get("input"), f"Undo: {r['label']}", "approved",
                        {"content": str(e), "is_error": True}, via="undo", undoes=r["id"], target=r.get("target"),
                        profile=where)
            raise
        if res.get("conflict"):
            return res
        self.record(r["tool"], r.get("input"), f"Undo: {r['label']}", "approved",
                    {"content": res["detail"], "is_error": False}, via="undo", undoes=r["id"], target=r.get("target"),
                    profile=where)
        rollback.forget(r["id"])
        return res


# ── speed tests this app ran ─────────────────────────────────────────────────
def record_speedtest(connection_id, point, via):
    return append(SPEED, dict(point, connection_id=connection_id, via=via))


def speedtests(connection_id, since_ms=0):
    return [r for r in read(SPEED) if r.get("connection_id") == connection_id and (r.get("at") or 0) >= since_ms]
