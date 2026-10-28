"""
Environment check, run by start.bat / start.command on every start (after the
private .venv is ready, before the app itself).

    .venv\\Scripts\\python backend\\envcheck.py        (Windows)
    .venv/bin/python backend/envcheck.py              (macOS / Linux)
    ... envcheck.py --open python-mac                 (just open that guide section)

It prints a short report: computer and OS, Python, packages, where passwords
are kept, folder access, whether the AI providers can be reached, and whether
Python has root certificates for the app's own downloads. Problems
come with a fix and a link to the right section of SETUP.html.
Exit code 1 means the app can't run (the launcher stops); warnings exit 0.

Standard library only (plus the packages it is checking), so it can report
on a half-installed environment instead of crashing on an import.
"""

import importlib.util
import platform
import socket
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SETUP_HTML = BASE_DIR / "SETUP.html"
MIN_PYTHON = (3, 10)
PACKAGES = [("anthropic", True), ("keyring", True), ("paramiko", True), ("cryptography", True), ("openai", False)]
AI_HOSTS = ("api.anthropic.com", "api.openai.com")
MAC_NAMES = {11: "Big Sur", 12: "Monterey", 13: "Ventura", 14: "Sonoma", 15: "Sequoia", 26: "Tahoe"}
OK, WARN, FAIL = "ok", "warn", "fail"


# ── opening the setup guide at a section ─────────────────────────────────────
def guide_url(anchor=None):
    return SETUP_HTML.as_uri() + (f"#{anchor}" if anchor else "")


def write_redirect(anchor):
    """Opening 'SETUP.html#section' straight from the OS usually loses the '#section'
    (Windows and macOS hand the browser a file path, not the URL). A tiny page that
    redirects to the full URL keeps it. -> path of that page."""
    url = guide_url(anchor)
    page = (f'<!doctype html><meta charset="utf-8"><title>Setup guide</title>'
            f'<meta http-equiv="refresh" content="0;url={url}">'
            f'<p><a href="{url}">Open the setup guide</a></p>\n')
    path = Path(tempfile.gettempdir()) / "flinchbot-open-guide.html"
    path.write_text(page, encoding="utf-8")
    return path


def open_guide(anchor=None):
    try:
        return webbrowser.open(write_redirect(anchor).as_uri())
    except Exception:
        return webbrowser.open(guide_url())


# ── the checks ────────────────────────────────────────────────────────────────
def describe_os():
    system = platform.system()
    arch = platform.machine() or "?"
    if system == "Windows":
        release, version = platform.win32_ver()[:2]
        try:
            build = int(version.split(".")[2])
        except (IndexError, ValueError):
            build = 0
        name = "Windows 11" if release in ("10", "11") and build >= 22000 else f"Windows {release}"
        edition = platform.win32_edition() if hasattr(platform, "win32_edition") else ""
        bits = "64-bit" if arch.endswith("64") else arch
        return f"{name} {edition or ''} ({version}, {bits})".replace("  ", " ")
    if system == "Darwin":
        ver = platform.mac_ver()[0] or "?"
        major = int(ver.split(".")[0]) if ver.split(".")[0].isdigit() else 0
        chip = "Apple silicon" if arch == "arm64" else "Intel"
        return f"macOS {MAC_NAMES.get(major, '')} {ver} ({chip})".replace("  ", " ")
    return f"{system} {platform.release()} ({arch})"


def check_os():
    system = platform.system()
    desc = describe_os()
    if system == "Windows":
        release, version = platform.win32_ver()[:2]
        if release not in ("10", "11"):
            return WARN, desc, "Windows 10 or 11 is needed; older versions may not work.", None
    elif system == "Darwin":
        ver = platform.mac_ver()[0] or "0"
        major = int(ver.split(".")[0]) if ver.split(".")[0].isdigit() else 0
        if major and major < 12:
            return WARN, desc, "macOS 12 (Monterey) or newer is recommended.", None
    return OK, desc, None, None


def check_python():
    v = sys.version_info
    where = sys.prefix
    if v < MIN_PYTHON:
        guide = "python-windows" if sys.platform.startswith("win") else "python-mac"
        return FAIL, f"{platform.python_version()} at {where}", \
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed.", guide
    return OK, f"{platform.python_version()} ({where})", None, None


def check_packages():
    """Installed and findable, read from package metadata (importing the AI SDKs
    just to check them would add seconds to every start)."""
    from importlib.metadata import PackageNotFoundError, version
    found, missing_required, missing_optional = [], [], []
    for name, required in PACKAGES:
        try:
            ver = version(name)
            if importlib.util.find_spec(name) is None:
                raise PackageNotFoundError(name)
            found.append(f"{name} {ver}")
        except Exception:
            (missing_required if required else missing_optional).append(name)
    if missing_required:
        return FAIL, ", ".join(found) or "none", (
            f"Missing: {', '.join(missing_required)}. Delete the .venv folder and start the app again "
            f"so they are reinstalled."), "start-it"
    if missing_optional:
        return WARN, ", ".join(found), f"Not installed: {', '.join(missing_optional)} (only needed for ChatGPT).", None
    return OK, ", ".join(found), None, None


def check_password_store():
    sys.path.insert(0, str(BASE_DIR / "backend"))
    try:
        import config_store as cfg
        name = cfg.secret_store_name()
        if cfg.secret_storage_kind() == "file":
            store = ("the macOS Keychain" if sys.platform == "darwin"
                     else "Windows Credential Manager" if sys.platform.startswith("win") else "a system keyring")
            return WARN, name, (f"{store} isn't available, so saved passwords go into data/secrets.json "
                                f"as plain text. Keep that folder private."), "where-stored"
        return OK, name, None, None
    except Exception as e:
        return WARN, "unknown", f"Couldn't check the password store ({e}).", "where-stored"


def check_folder():
    data = BASE_DIR / "data"
    try:
        data.mkdir(exist_ok=True)
        probe = data / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return OK, f"{BASE_DIR} (writable)", None, None
    except OSError as e:
        return FAIL, str(BASE_DIR), (f"The app can't save its settings here ({e.strerror or e}). Move the "
                                     f"folder somewhere you can write to, e.g. Documents, and start it again."), \
            "get-the-app"


def _try_resolve(host, out):
    try:
        out[host] = bool(socket.getaddrinfo(host, 443))
    except OSError:
        out[host] = False


def check_internet(timeout=3.0):
    """Name lookup only (nothing is sent): can this computer find the AI providers?
    All lookups run at once, so a dead connection costs at most `timeout` seconds."""
    out = {}
    threads = [threading.Thread(target=_try_resolve, args=(h, out), daemon=True) for h in AI_HOSTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    ok = [h for h in AI_HOSTS if out.get(h)]
    if len(ok) == len(AI_HOSTS):
        return OK, "AI providers reachable", None, None
    if ok:
        return OK, f"{', '.join(ok)} reachable", None, None
    return WARN, "AI providers not reachable", ("This computer can't look up api.anthropic.com or api.openai.com. "
                                                "The app will start, but the AI won't answer until the "
                                                "internet connection works."), None


def check_certificates():
    """Root certificates for the app's own downloads (update checks, the price list).
    Offline: counts what the app's TLS setup loads, nothing is sent. The AI providers
    don't depend on this (their SDKs read the system's trust store themselves)."""
    sys.path.insert(0, str(BASE_DIR / "backend"))
    import config_store as cfg
    n, certifi_ver, folder = cfg.ssl_roots()
    if n or folder:
        where = [f"certifi {certifi_ver}"] if certifi_ver else []
        if sys.platform.startswith("win"):
            where.append("Windows store")
        elif folder:
            where.append(folder)
        return OK, f"{n} root certificates" + (f" ({' + '.join(where)})" if where else ""), None, None
    if sys.platform == "darwin":
        fix = ("Python on this Mac has no root certificates, so update checks and price downloads fail with "
               "CERTIFICATE_VERIFY_FAILED. In Finder, open Applications > Python 3.x and double-click "
               "Install Certificates.command, then start the app again. (Or delete the .venv folder and start "
               "the app again, so the certifi package is reinstalled.)")
    else:
        fix = ("Python found no root certificates, so update checks and price downloads will fail with "
               "CERTIFICATE_VERIFY_FAILED. Delete the .venv folder and start the app again, so the certifi "
               "package is reinstalled.")
    return WARN, "none found", fix, "python-mac" if sys.platform == "darwin" else "start-it"


def check_nmap():
    """Optional: Nmap powers device scans in the device detective and the security scorecard."""
    import nmap_scan                    # stdlib only, next to this file
    path = nmap_scan.find()
    if path:
        return OK, f"found ({path})", None, None
    return OK, "not installed (optional: lets the app scan devices; get it from nmap.org)", None, None


CHECKS = [("Computer", check_os), ("Python", check_python), ("Packages", check_packages),
          ("Passwords", check_password_store), ("Folder", check_folder), ("Internet", check_internet),
          ("Certs", check_certificates), ("Nmap", check_nmap)]


def run():
    """-> list of (label, status, detail, problem, guide_anchor)."""
    out = []
    for label, fn in CHECKS:
        try:
            status, detail, problem, anchor = fn()
        except Exception as e:      # a check must never stop the app from starting
            status, detail, problem, anchor = WARN, "check failed", str(e), None
        out.append((label, status, detail, problem, anchor))
    return out


def report(results, stream=None):
    stream = stream or sys.stdout
    mark = {OK: "  ok ", WARN: "  !! ", FAIL: "  XX "}
    w = stream.write
    w("\n  Environment check\n")
    for label, status, detail, problem, anchor in results:
        w(f"{mark[status]}{label:<10} {detail}\n")
        if problem:
            w(f"       -> {problem}\n")
            if anchor:
                w(f"          Guide: {guide_url(anchor)}\n")
    worst = FAIL if any(r[1] == FAIL for r in results) else WARN if any(r[1] == WARN for r in results) else OK
    w({OK: "  Ready.\n", WARN: "  Ready, with the warnings above.\n",
       FAIL: "  The app can't start until the problems marked XX are fixed.\n"}[worst])
    return worst


def main(argv):
    if len(argv) >= 2 and argv[0] == "--open":
        open_guide(argv[1])
        return 0
    results = run()
    worst = report(results)
    if worst == FAIL:
        anchor = next((r[4] for r in results if r[1] == FAIL and r[4]), None)
        if anchor and sys.stdin and sys.stdin.isatty():
            try:
                ans = input("\n  Open the setup guide at the section that explains the fix? [Y/n] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans in ("", "y", "yes"):
                open_guide(anchor)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
