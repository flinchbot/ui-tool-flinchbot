"""
Install or update the app's packages into .venv, and say exactly what changed.

Run by start.bat / start.command when requirements.txt differs from what was
last installed (so on the first start, and after an update that changes it):

    .venv\\Scripts\\python backend\\install_deps.py      (Windows)
    .venv/bin/python backend/install_deps.py            (macOS / Linux)

It notes which packages .venv holds, runs pip (quietly: pip's own errors still
show), then lists every package that was added or upgraded, with its version and
why it is there: one this app asks for (with what it's for, from the comment in
requirements.txt) or one another package needs. The same list is appended to
.venv/install-log.txt with the date.

Standard library only: it runs before any of the packages exist.
Exit code: pip's, so the launcher stops if the install failed.
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = BASE_DIR / "requirements.txt"
LOG = Path(sys.prefix) / "install-log.txt"
PIP = [sys.executable, "-m", "pip", "--disable-pip-version-check"]

# Lists every installed distribution as {normalised name: (display name, version, [required names])}.
# Run in a fresh interpreter so the "after" picture isn't served from this process's caches.
_SNAPSHOT = r"""
import json, re
from importlib.metadata import distributions
norm = lambda n: re.sub(r"[-_.]+", "-", n or "").lower()
out = {}
for d in distributions():
    name = d.metadata.get("Name")
    if not name:
        continue
    reqs = []
    for r in d.requires or []:
        if "extra ==" in r:          # optional extras aren't installed unless asked for
            continue
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", r)
        if m:
            reqs.append(norm(m.group(1)))
    out[norm(name)] = [name, d.version, reqs]
print(json.dumps(out))
"""


def norm(name):
    return re.sub(r"[-_.]+", "-", name or "").lower()


def requested():
    """-> {normalised name: purpose} from requirements.txt, in file order."""
    out, comment = {}, ""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            comment = s.lstrip("# ").split(" - ")[0].strip()
        elif s:
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", s)
            if m:
                out[norm(m.group(1))] = comment
            comment = ""
    return out


def snapshot():
    try:
        r = subprocess.run([sys.executable, "-c", _SNAPSHOT], capture_output=True, text=True, timeout=60)
        return {k: tuple(v) for k, v in json.loads(r.stdout).items()}
    except Exception:
        return {}


def changes(before, after, wanted):
    """-> list of (display name, old version or None, new version, why), the app's own packages first."""
    rows = []
    for key, (name, ver, _) in after.items():
        old = before.get(key)
        if old and old[1] == ver:
            continue
        if key in wanted:
            why = f"for this app: {wanted[key]}" if wanted[key] else "for this app"
        elif key == "pip":
            why = "the installer itself"
        else:
            parents = sorted(after[p][0] for p, (_, _, reqs) in after.items() if key in reqs)
            why = f"needed by {', '.join(parents)}" if parents else "needed by another package"
        rows.append((name, old[1] if old else None, ver, why))
    order = list(wanted)
    rows.sort(key=lambda r: (order.index(norm(r[0])) if norm(r[0]) in order else len(order), norm(r[0])))
    return rows


def render(rows, unchanged):
    if not rows:
        return [f"  All {unchanged} packages were already up to date."]
    width = max(len(r[0]) for r in rows)
    vwidth = max(len(f"{r[1]} -> {r[2]}" if r[1] else r[2]) for r in rows)
    lines = [f"  Installed or updated {len(rows)} package{'s' if len(rows) != 1 else ''}:"]
    for name, old, new, why in rows:
        ver = f"{old} -> {new}" if old else new
        lines.append(f"    {name:<{width}}  {ver:<{vwidth}}  {why}")
    if unchanged:
        lines.append(f"  {unchanged} other package{'s were' if unchanged != 1 else ' was'} already up to date.")
    return lines


def main():
    before = snapshot()
    print("  Checking the installer (pip) ...", flush=True)
    subprocess.run(PIP + ["install", "-q", "--upgrade", "pip"])          # a failure here is not fatal
    print(f"  Installing the packages listed in requirements.txt and what they need ...", flush=True)
    rc = subprocess.run(PIP + ["install", "-q", "-r", str(REQUIREMENTS)]).returncode
    after = snapshot()
    rows = changes(before, after, requested())
    unchanged = sum(1 for k, v in after.items() if before.get(k) and before[k][1] == v[1])
    lines = render(rows, unchanged)
    print("\n".join(lines), flush=True)
    if rc:
        print("  pip reported a problem (see above), so the list may be incomplete.", flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}  "
                    f"Python {sys.version.split()[0]}  pip exit {rc}\n" + "\n".join(lines) + "\n\n")
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
