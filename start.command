#!/bin/bash
# ===========================================================================
#  UniFi x flinchbot - double-click to start on a Mac (Linux: ./start.command).
#  First run: creates a private Python environment (.venv) and installs what
#  the app needs. Later runs start in a few seconds.
#  Options are passed through, e.g.:  ./start.command --port 8090
#  Windows uses start.bat instead; both share the same .env and data/ folder.
# ===========================================================================
cd "$(dirname "$0")" || exit 1
printf '\033]0;UniFi x flinchbot\007'      # window title

fail() {
  echo
  printf '  %s\n' "$@"
  echo
  read -r -p "  Press Return to close this window. " _
  exit 1
}

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) fail "This is the Mac/Linux launcher. On Windows, double-click start.bat instead." ;;
esac

# --- find Python 3.10+ ------------------------------------------------------
# On a Mac without the Xcode command-line tools, /usr/bin/python3 is only a stub
# that pops up an "install developer tools" dialog, so it's skipped unless real.
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  if [ "$(uname)" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
    continue
  fi
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$p"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer was not found." \
  "Install it from https://www.python.org/downloads/ (see SETUP.md, step 1)," \
  "then double-click start.command again."

# --- create the private environment once -------------------------------------
# A .venv copied over from a Windows PC has no bin/python: rebuild it (it only
# holds downloaded packages, nothing of yours).
if [ ! -x ".venv/bin/python" ]; then
  echo "  First run: creating a private Python environment in .venv ..."
  "$PY" -m venv --clear .venv || fail "Could not create the .venv folder. Make sure this folder" \
    "isn't read-only and that Python installed correctly (see SETUP.md, step 1)."
fi

# --- install / update requirements only when requirements.txt changed ---------
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "  Installing required packages - this needs internet and takes a minute ..."
  if .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip &&
     .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
    cp requirements.txt .venv/requirements.installed
  else
    rm -f .venv/requirements.installed
    fail "Installing packages failed. Check this computer's internet connection and try again." \
      "If it keeps failing, delete the .venv folder and run start.command again."
  fi
fi

# --- run the app (opens your browser automatically) ---------------------------
exec .venv/bin/python backend/server.py "$@"
