#!/bin/bash
# ===========================================================================
#  UniFi x flinchbot - double-click to start on a Mac (Linux: ./start.command).
#  Checks this computer first (Python, folder, packages) and offers fixes.
#  First run: creates a private Python environment (.venv) and installs what
#  the app needs. Later runs start in a few seconds.
#  Options are passed through, e.g.:  ./start.command --port 8090
#  Windows uses start.bat instead; both share the same .env and data/ folder.
#  FB_DRY_RUN=1 makes the "open ..." offers write their files but open nothing.
# ===========================================================================

fail() {
  echo
  printf '  %s\n' "$@"
  echo
  read -r -p "  Press Return to close this window. " _
  exit 1
}

ask_yes() {      # ask_yes "Question?"  -> true unless the answer starts with n/N
  local a
  read -r -p "  $1 [Y/n] " a
  case "$a" in [nN]*) return 1 ;; *) return 0 ;; esac
}

open_url() {
  [ -n "$FB_DRY_RUN" ] && return 0
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 &
  else echo "  Open this in your browser: $1"
  fi
}

# Open SETUP.html at a section. macOS drops the "#section" when it opens a local
# file, so write a tiny page that redirects to SETUP.html#section and open that.
open_guide() {
  local p="$FB_DIR/SETUP.html" url page
  p="${p//\%/%25}"; p="${p// /%20}"; p="${p//\#/%23}"; p="${p//\"/%22}"; p="${p//\?/%3F}"
  url="file://$p#$1"
  page="${TMPDIR:-/tmp}/flinchbot-open-guide.html"
  printf '<!doctype html><meta charset="utf-8"><title>Setup guide</title><meta http-equiv="refresh" content="0;url=%s"><p><a href="%s">Open the setup guide</a></p>\n' \
    "$url" "$url" > "$page" || { open_url "$FB_DIR/SETUP.html"; return; }
  open_url "$page"
}

# Python installed but not usable / not installed: explain and offer the fix.
no_python() {
  local old="" c p v url anchor
  for c in python3 python; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if [ "$(uname)" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    v="$("$p" -c 'import platform; print(platform.python_version())' 2>/dev/null)" && { old="$v"; break; }
  done
  echo
  if [ -n "$old" ]; then
    echo "  Python $old is installed, but this app needs Python 3.10 or newer."
  else
    echo "  Python isn't installed on this computer (or it can't be found)."
  fi
  echo "  The app needs Python 3.10 or newer. It's free and takes a few minutes to install."
  if [ "$(uname)" = "Darwin" ]; then
    url="https://www.python.org/downloads/macos/"; anchor="python-mac"
    command -v brew >/dev/null 2>&1 && echo "  Tip: with Homebrew you can also install it from here:  brew install python"
  else
    url="https://www.python.org/downloads/"; anchor="install-python"
    echo "  On Linux, install it with your package manager, e.g.:  sudo apt install python3 python3-venv"
  fi
  echo
  if ask_yes "Open the Python download page and the setup guide now?"; then
    open_url "$url"
    open_guide "$anchor"
    echo "  Opened. After installing Python, double-click start.command again."
  fi
  fail "The app can't start without Python 3.10 or newer."
}

[ -n "$FB_SOURCE_ONLY" ] && return 0 2>/dev/null     # tests: load the functions only

cd "$(dirname "$0")" || exit 1
FB_DIR="$(pwd -P)"
printf '\033]0;UniFi x flinchbot\007'      # window title
echo
echo "  UniFi x flinchbot - checking this computer ..."

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) fail "This is the Mac/Linux launcher. On Windows, double-click start.bat instead." ;;
esac

# --- can the app write here? -------------------------------------------------
if ! { : > .write-test.tmp; } 2>/dev/null; then
  echo
  echo "  This folder is read-only, so the app can't install what it needs or save"
  echo "  its settings here. Move the whole folder somewhere you can write to, such"
  echo "  as Documents, and start it again."
  echo
  ask_yes "Open the setup guide at that step?" && open_guide "get-the-app"
  fail "The app can't start from a read-only folder."
fi
rm -f .write-test.tmp

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
[ -n "$PY" ] || no_python

# --- create the private environment once -------------------------------------
# A .venv copied over from a Windows PC has no bin/python: rebuild it (it only
# holds downloaded packages, nothing of yours).
if [ ! -x ".venv/bin/python" ]; then
  echo "  First run: creating a private Python environment in .venv ..."
  if ! "$PY" -m venv --clear .venv; then
    echo
    echo "  Could not create the .venv folder. On Linux, install the venv module"
    echo "  (e.g. sudo apt install python3-venv); on a Mac, reinstall Python from python.org."
    ask_yes "Open the setup guide at the Python step?" && open_guide "install-python"
    fail "The app can't start without its .venv folder."
  fi
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

# --- environment report (computer, Python, packages, passwords, internet) ----
.venv/bin/python backend/envcheck.py || fail "Fix the problems marked XX above, then start the app again."

# --- run the app (opens your browser automatically) ---------------------------
exec .venv/bin/python backend/server.py "$@"
