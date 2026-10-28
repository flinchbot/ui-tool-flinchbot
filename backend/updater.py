"""
Updates from GitHub: is there a newer version, and install it over this folder.

The app's version is APP_VERSION in backend/server.py. "Newer" means the
APP_VERSION on the repository's main branch is higher, so pushing a commit that
bumps the version is a release. Installing downloads that commit's ZIP from
GitHub and writes its files over the app folder:

  - .env, data/, .venv/ and .git/ are never touched;
  - files the new version no longer has are left alone ("over the top");
  - every file that gets replaced is first copied into data/updates/backup-*.zip,
    so a roll back restores the previous version exactly.

The launcher then restarts the app (exit code 75, see start.bat / start.command),
which also installs any new requirements. A git checkout is never overwritten:
it is told to `git pull` instead.

A private repository needs a GitHub token: one saved in the app, GITHUB_TOKEN /
GH_TOKEN, or the GitHub CLI's (`gh auth token`). It is only sent to api.github.com.

If a new version won't start:   .venv/bin/python backend/updater.py --rollback
(Windows: .venv\\Scripts\\python backend\\updater.py --rollback)
"""

import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_store as cfg            # noqa: E402

DEFAULT_REPO = "flinchbot/ui-tool-flinchbot"
DEFAULT_BRANCH = "main"
API = os.environ.get("FB_UPDATE_API", "https://api.github.com")
RESTART_CODE = 75                     # the launchers start again on this exit code
CHECK_EVERY = 24 * 3600
KEEP_BACKUPS = 3
MAX_ZIP = 100_000_000
MAX_UNPACKED = 300_000_000
PROTECTED = {".env", "data", ".venv", ".git"}     # top-level names an update never writes
REQUIRED = ("backend/server.py", "frontend/index.html", "start.bat", "start.command")
TOKEN_ID = "app:github-token"
VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', re.M)

_lock = threading.Lock()


class UpdateError(ValueError):
    def __init__(self, msg, kind=""):
        super().__init__(msg)
        self.kind = kind


# ── where things are ──────────────────────────────────────────────────────────
def repo():
    r = (cfg.get_setting("UPDATE_REPO") or DEFAULT_REPO).strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r):
        raise UpdateError(f"UPDATE_REPO in .env should look like owner/name, not {r!r}.")
    return r


def branch():
    return (cfg.get_setting("UPDATE_BRANCH") or DEFAULT_BRANCH).strip()


def updates_dir(base_dir):
    return os.path.join(base_dir, "data", "updates")


def _state_path(base_dir):
    return os.path.join(updates_dir(base_dir), "state.json")


def is_git_checkout(base_dir):
    return os.path.exists(os.path.join(base_dir, ".git"))


# ── versions ──────────────────────────────────────────────────────────────────
def parse_version(v):
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def version_in(source):
    m = VERSION_RE.search(source or "")
    return m.group(1) if m else None


# ── GitHub token ──────────────────────────────────────────────────────────────
def _saved_token():
    try:
        return cfg.get_secret(TOKEN_ID) or None
    except Exception:
        return None


def _gh_token():
    exe = shutil.which("gh")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "auth", "token", "--hostname", "github.com"], capture_output=True,
                           text=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tok = r.stdout.strip()
        return tok if r.returncode == 0 and tok else None
    except (OSError, subprocess.SubprocessError):
        return None


def token():
    """-> (token, where it came from) or (None, None)."""
    t = _saved_token()
    if t:
        return t, "saved in the app"
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name], name
    t = _gh_token()
    if t:
        return t, "GitHub CLI (gh)"
    return None, None


def save_token(tok):
    tok = (tok or "").strip()
    if not re.fullmatch(r"(ghp_|github_pat_|gho_|ghu_)[A-Za-z0-9_]{20,255}", tok):
        raise UpdateError("That doesn't look like a GitHub token (they start with ghp_ or github_pat_).")
    _get_json(f"/repos/{repo()}", tok)      # raises a clear error if it can't see the repository
    cfg.set_secret(TOKEN_ID, tok)


def delete_token():
    try:
        kr = cfg._keyring()
        if kr:
            kr.delete_password(cfg.KEYRING_SERVICE, "profile:" + TOKEN_ID)
            return
    except Exception:
        pass
    try:
        data = cfg._read_secret_file()
        if data.pop(TOKEN_ID, None) is not None:
            cfg._atomic_write(cfg.SECRETS_PATH, json.dumps(data, indent=2))
    except Exception:
        pass


# ── talking to GitHub ─────────────────────────────────────────────────────────
class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """The ZIP download redirects to codeload.github.com with its own short-lived
    link; the token stays with the API host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for k in list(new.headers):
                if k.lower() == "authorization":
                    del new.headers[k]
        return new


def _ssl_context():
    try:
        import certifi                      # python.org Python on a Mac has no CA list without it
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _request(path, tok, accept="application/vnd.github+json", timeout=20, limit=5_000_000):
    url = path if path.startswith("http") else API.rstrip("/") + path
    headers = {"User-Agent": "unifi-flinchbot-updater", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    opener = urllib.request.build_opener(_DropAuthOnRedirect(), urllib.request.HTTPSHandler(context=_ssl_context()))
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 401:
            raise UpdateError("GitHub refused the token (expired or revoked). Save a new one, or remove it.", "auth")
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            raise UpdateError("GitHub's limit for checks without a token was reached. Try again in an hour, "
                              "or save a GitHub token.", "rate")
        if e.code in (403, 404):
            if not tok:
                raise UpdateError(f"GitHub didn't find {repo()}. If the repository is private, the app needs a "
                                  f"GitHub token that can read it.", "auth")
            raise UpdateError(f"GitHub didn't find {repo()} with the token in use. Check that the token can "
                              f"read this repository (Contents: read-only).", "auth")
        raise UpdateError(f"GitHub answered HTTP {e.code} ({e.reason}).", "http")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise UpdateError(f"Couldn't reach GitHub ({reason}). Check this computer's internet connection.", "net")
    if len(data) > limit:
        raise UpdateError("GitHub sent more data than expected; the update was stopped.", "http")
    return data


def _get_json(path, tok):
    try:
        return json.loads(_request(path, tok))
    except ValueError as e:
        if isinstance(e, UpdateError):
            raise
        raise UpdateError("GitHub sent something that isn't JSON.", "http")


# ── checking ──────────────────────────────────────────────────────────────────
def _notes(commits, current):
    """Commits since the one that set the current version (newest first)."""
    out = []
    for c in commits:
        msg = ((c.get("commit") or {}).get("message") or "").strip()
        subject, _, body = msg.partition("\n")
        if current and re.match(rf"v?{re.escape(current)}\b", subject):
            break
        out.append({"sha": (c.get("sha") or "")[:7], "subject": subject.strip(),
                    "body": body.strip(), "date": ((c.get("commit") or {}).get("committer") or {}).get("date")})
    return out[:30]


def check(current):
    """Ask GitHub for the newest version. -> dict (raises UpdateError)."""
    tok, _ = token()
    r, b = repo(), branch()
    commits = _get_json(f"/repos/{r}/commits?sha={urllib.parse.quote(b)}&per_page=30", tok)
    if not isinstance(commits, list) or not commits:
        raise UpdateError(f"GitHub has no commits on {r} branch {b}.", "http")
    sha = commits[0]["sha"]
    src = _request(f"/repos/{r}/contents/backend/server.py?ref={sha}", tok,
                   accept="application/vnd.github.raw").decode("utf-8", "replace")
    latest = version_in(src)
    if not latest:
        raise UpdateError("Couldn't find the version number in the latest code on GitHub.", "http")
    return {"latest": latest, "sha": sha, "date": ((commits[0].get("commit") or {}).get("committer") or {}).get("date"),
            "newer": is_newer(latest, current), "notes": _notes(commits, current) if is_newer(latest, current) else []}


def _load_state(base_dir):
    try:
        with open(_state_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(base_dir, data):
    os.makedirs(updates_dir(base_dir), exist_ok=True)
    cfg._atomic_write(_state_path(base_dir), json.dumps(data, indent=2))


def set_auto(base_dir, on):
    with _lock:
        s = _load_state(base_dir)
        s["auto"] = bool(on)
        _save_state(base_dir, s)


def status(current, base_dir, force=False):
    """What the Updates window shows. Checks GitHub when asked to, or once a day
    when automatic checks are on; otherwise answers from the last check."""
    with _lock:
        s = _load_state(base_dir)
        auto = s.get("auto", True)
        stale = time.time() - float(s.get("checked_at_ts") or 0) > CHECK_EVERY
        last = s.get("last") or {}
        if last.get("current") != current:          # updated (or rolled back) since: that answer is old
            stale = True
        if force or (auto and stale):
            try:
                last = dict(check(current), current=current)
                s["error"], s["error_kind"] = None, None
            except UpdateError as e:
                s["error"], s["error_kind"] = str(e), e.kind
            s["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            s["checked_at_ts"] = time.time()
            s["last"] = last
            _save_state(base_dir, s)
        _, source = token()
        newer = bool(last.get("latest") and is_newer(last["latest"], current))
        return {"current": current, "latest": last.get("latest"), "newer": newer, "sha": last.get("sha"),
                "date": last.get("date"), "notes": last.get("notes", []) if newer else [],
                "checked_at": s.get("checked_at"), "error": s.get("error"), "error_kind": s.get("error_kind"),
                "auto": auto, "repo": repo(), "branch": branch(), "token_source": source,
                "token_saved": bool(_saved_token()), "git_checkout": is_git_checkout(base_dir),
                "installed": s.get("installed"), "backups": list_backups(base_dir)}


# ── installing ────────────────────────────────────────────────────────────────
def _safe_rel(rel):
    """A zip member's path inside the app folder, or None if it must not be written."""
    if not rel or rel.endswith("/") or "\\" in rel or "\0" in rel:
        return None
    parts = rel.split("/")
    if rel.startswith("/") or re.match(r"[A-Za-z]:", rel) or any(p in ("", ".", "..") for p in parts):
        raise UpdateError(f"The download contains an unsafe path ({rel!r}); the update was stopped.", "bad_zip")
    if parts[0] in PROTECTED:
        return None
    if any(p == "__pycache__" for p in parts):
        return None
    return rel


def _members(zf):
    """-> {relative path: ZipInfo} for the files to write. GitHub ZIPs hold one top folder."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    tops = {i.filename.split("/", 1)[0] for i in zf.infolist()}
    if len(tops) != 1 or not infos:
        raise UpdateError("The download isn't a GitHub source ZIP (expected one top folder).", "bad_zip")
    top = tops.pop() + "/"
    out, total = {}, 0
    for i in infos:
        if not i.filename.startswith(top):
            raise UpdateError("The download isn't a GitHub source ZIP.", "bad_zip")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:      # symlink
            continue
        rel = _safe_rel(i.filename[len(top):])
        if rel:
            out[rel] = i
            total += i.file_size
    if total > MAX_UNPACKED:
        raise UpdateError("The download is far larger than this app; the update was stopped.", "bad_zip")
    missing = [r for r in REQUIRED if r not in out]
    if missing:
        raise UpdateError(f"The download is missing {', '.join(missing)}; it isn't this app.", "bad_zip")
    return out


def _fix_file(dest, rel, info):
    if rel.endswith(".bat"):                       # cmd.exe misreads a batch file without CRLF
        with open(dest, "rb") as f:
            raw = f.read()
        fixed = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        if fixed != raw:
            with open(dest, "wb") as f:
                f.write(fixed)
    if os.name != "nt":
        mode = (info.external_attr >> 16) & 0o777
        if rel.endswith((".command", ".sh")) or mode & 0o100:
            os.chmod(dest, 0o755)


def _backup_name(old_version):
    return f"backup-{old_version or 'unknown'}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"


def apply_zip(zip_bytes, base_dir, old_version, expect_version=None):
    """Write a GitHub source ZIP over base_dir. -> {"written", "added", "backup"}.
    All files are staged first, then swapped in; on a failure halfway, the
    files already swapped are put back from the backup."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise UpdateError("The download isn't a valid ZIP file.", "bad_zip")
    with zf:
        members = _members(zf)
        if expect_version:
            got = version_in(zf.read(members["backend/server.py"]).decode("utf-8", "replace"))
            if got != expect_version:
                raise UpdateError(f"The download is version {got}, not {expect_version}; the update was stopped.",
                                  "bad_zip")
        os.makedirs(updates_dir(base_dir), exist_ok=True)
        name = _backup_name(old_version)
        added = [r for r in members if not os.path.exists(os.path.join(base_dir, r))]
        with zipfile.ZipFile(os.path.join(updates_dir(base_dir), name), "w", zipfile.ZIP_DEFLATED) as bz:
            for rel in members:
                path = os.path.join(base_dir, rel)
                if os.path.isfile(path):
                    bz.write(path, rel)
            bz.writestr("__update__.json", json.dumps({"version": old_version, "added": added,
                                                        "at": datetime.now().astimezone().isoformat(timespec="seconds")}))
        staged = []
        try:
            for rel, info in members.items():
                dest = os.path.join(base_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".fb-new"
                with zf.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                _fix_file(tmp, rel, info)
                staged.append((tmp, dest))
        except OSError as e:
            for tmp, _ in staged:
                _remove(tmp)
            raise UpdateError(f"Couldn't write the new files ({e.strerror or e}); nothing was changed.", "write")
        done = []
        try:
            for tmp, dest in staged:
                os.replace(tmp, dest)
                done.append(dest)
        except OSError as e:
            for tmp, _ in staged:
                _remove(tmp)
            restore_backup(base_dir, name, only=done)
            raise UpdateError(f"Couldn't replace {os.path.relpath(dest, base_dir)} ({e.strerror or e}); "
                              f"the previous version was put back.", "write")
    _prune_backups(base_dir)
    return {"written": len(members), "added": len(added), "backup": name}


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def install(current, base_dir):
    """Download the newest version from GitHub and install it. -> dict."""
    if is_git_checkout(base_dir):
        raise UpdateError("This app folder is a git checkout, so it isn't overwritten. Run `git pull` "
                          "in the folder instead, then restart the app.", "git")
    with _lock:
        info = check(current)
        if not info["newer"]:
            raise UpdateError(f"Already up to date (v{current}).", "current")
        tok, _ = token()
        data = _request(f"/repos/{repo()}/zipball/{info['sha']}", tok, accept="*/*", timeout=120, limit=MAX_ZIP)
        result = apply_zip(data, base_dir, current, expect_version=info["latest"])
        s = _load_state(base_dir)
        s["installed"] = {"version": info["latest"], "from": current, "sha": info["sha"],
                          "at": datetime.now().astimezone().isoformat(timespec="seconds"), "backup": result["backup"]}
        _save_state(base_dir, s)
    return dict(result, version=info["latest"], previous=current)


# ── backups / roll back ───────────────────────────────────────────────────────
def list_backups(base_dir):
    d = updates_dir(base_dir)
    out = []
    try:
        names = sorted((n for n in os.listdir(d) if re.fullmatch(r"backup-[\w.-]+\.zip", n)), reverse=True)
    except OSError:
        return out
    for n in names:
        meta = {}
        try:
            with zipfile.ZipFile(os.path.join(d, n)) as z:
                meta = json.loads(z.read("__update__.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        out.append({"name": n, "version": meta.get("version"), "at": meta.get("at")})
    out.sort(key=lambda b: b.get("at") or "", reverse=True)
    return out


def _prune_backups(base_dir):
    for b in list_backups(base_dir)[KEEP_BACKUPS:]:
        _remove(os.path.join(updates_dir(base_dir), b["name"]))


def restore_backup(base_dir, name, only=None):
    """Put back the files a backup holds and remove the files that update added.
    only: restore just these destination paths (used when an install fails halfway).
    -> the version the backup holds."""
    if not re.fullmatch(r"backup-[\w.-]+\.zip", name or ""):
        raise UpdateError("Unknown backup.")
    path = os.path.join(updates_dir(base_dir), name)
    if not os.path.isfile(path):
        raise UpdateError("That backup no longer exists.")
    want = {os.path.normcase(os.path.abspath(p)) for p in only} if only is not None else None
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("__update__.json"))
        for info in z.infolist():
            if info.filename == "__update__.json" or info.is_dir():
                continue
            rel = _safe_rel(info.filename)
            if not rel:
                continue
            dest = os.path.join(base_dir, *rel.split("/"))
            if want is not None and os.path.normcase(os.path.abspath(dest)) not in want:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".fb-new"
            with z.open(info) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            _fix_file(tmp, rel, info)
            os.replace(tmp, dest)
        for rel in meta.get("added") or []:
            rel = _safe_rel(rel)
            if not rel:
                continue
            dest = os.path.join(base_dir, *rel.split("/"))
            if want is not None and os.path.normcase(os.path.abspath(dest)) not in want:
                continue
            if os.path.isfile(dest):
                _remove(dest)
    return meta.get("version")


def rollback(base_dir, name):
    if is_git_checkout(base_dir):
        raise UpdateError("This app folder is a git checkout; use git to go back to another version.", "git")
    with _lock:
        version = restore_backup(base_dir, name)
        s = _load_state(base_dir)
        s["installed"] = {"version": version, "rolled_back": True, "backup": name,
                          "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        _save_state(base_dir, s)
    return version


# ── restart hand-over ─────────────────────────────────────────────────────────
def launched_by_launcher():
    """start.bat / start.command start the app again on RESTART_CODE."""
    return os.environ.get("FB_LAUNCHER") in ("start.bat", "start.command")


def save_handover(base_dir, app_token, port):
    """Lets the restarted app keep the page's token, so the open tab carries on."""
    os.makedirs(updates_dir(base_dir), exist_ok=True)
    cfg._atomic_write(os.path.join(updates_dir(base_dir), "restart.json"),
                      json.dumps({"token": app_token, "port": port, "at": time.time()}))


def take_handover(base_dir, port, max_age=300):
    """-> the previous token if this start is a restart after an update, else None."""
    path = os.path.join(updates_dir(base_dir), "restart.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    finally:
        _remove(path)
    if data.get("port") == port and time.time() - float(data.get("at") or 0) < max_age:
        tok = data.get("token")
        return tok if isinstance(tok, str) and len(tok) >= 32 else None
    return None


def main(argv):
    if argv[:1] == ["--rollback"]:
        backups = list_backups(cfg.BASE_DIR)
        if not backups:
            print("  No update backups in data/updates.")
            return 1
        b = backups[0]
        version = rollback(cfg.BASE_DIR, b["name"])
        print(f"  Put back version {version} (from {b['name']}). Start the app again.")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
