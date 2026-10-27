"""
Settings (.env) and UniFi connection profiles.

Where things live (all relative to the project folder, so the whole folder
can be copied to another PC):

  .env                    app settings + AI provider API keys (never commit)
  data/connections.json   UniFi connection profiles, NO passwords
  Windows Credential Manager (service "unifi-claude" - name kept from the
                          app's first version so saved passwords keep working)
                          UniFi passwords / API keys
  data/secrets.json       fallback store, used only if Credential Manager
                          is unavailable (plaintext - the UI warns about it)

Real environment variables override .env values (standard dotenv behaviour).
"""

import json
import os
import re
import tempfile
import threading
import uuid

BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH      = os.path.join(BASE_DIR, ".env")
DATA_DIR      = os.path.join(BASE_DIR, "data")
PROFILES_PATH = os.path.join(DATA_DIR, "connections.json")
SECRETS_PATH  = os.path.join(DATA_DIR, "secrets.json")
FRONTEND_DIR  = os.path.join(BASE_DIR, "frontend")

KEYRING_SERVICE = "unifi-claude"

DEFAULTS = {
    "AI_PROVIDER":       "anthropic",          # "anthropic" (Claude) or "openai" (ChatGPT)
    "ANTHROPIC_API_KEY": "",
    "CLAUDE_MODEL":      "claude-sonnet-5-5",
    "OPENAI_API_KEY":    "",
    "OPENAI_MODEL":      "gpt-6.1-sol",
    "SERVER_HOST":       "127.0.0.1",
    "SERVER_PORT":       "8080",
    "MAX_TOOL_ROUNDS":   "15",
    "MAX_OUTPUT_TOKENS": "16000",
}

# Keys the settings UI is allowed to write. Anything else in .env is preserved.
EDITABLE_KEYS = {"AI_PROVIDER", "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "SERVER_PORT"}

# Per-provider .env keys
PROVIDER_KEYS = {
    "anthropic": {"key": "ANTHROPIC_API_KEY", "model": "CLAUDE_MODEL"},
    "openai":    {"key": "OPENAI_API_KEY",    "model": "OPENAI_MODEL"},
}


def active_provider():
    p = (get_setting("AI_PROVIDER") or "anthropic").strip().lower()
    return p if p in PROVIDER_KEYS else "anthropic"


def provider_key(provider):
    return get_setting(PROVIDER_KEYS[provider]["key"])


def provider_model(provider):
    return get_setting(PROVIDER_KEYS[provider]["model"])

_lock = threading.RLock()


# ── atomic file helpers ───────────────────────────────────────────────────────
def _atomic_write(path, text):
    """Write via temp file + rename so a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── .env ──────────────────────────────────────────────────────────────────────
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    # strip trailing inline comment on unquoted values
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def _quote(v):
    v = str(v)
    if v == "" or re.search(r'[\s#"\'\\=]', v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def read_env_file():
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    with open(ENV_PATH, encoding="utf-8-sig") as f:
        for line in f:
            m = _ENV_LINE.match(line)
            if m:
                values[m.group(1)] = _unquote(m.group(2))
    return values


def write_env_file(updates):
    """Update/add keys in .env, keeping comments, ordering and unknown keys."""
    with _lock:
        lines = []
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        else:
            lines = ["# UniFi x AI settings - created by the Settings screen.",
                     "# Keep this file private: it contains your AI provider API key(s)."]
        remaining = dict(updates)
        out = []
        for line in lines:
            m = _ENV_LINE.match(line)
            if m and m.group(1) in remaining:
                out.append(f"{m.group(1)}={_quote(remaining.pop(m.group(1)))}")
            else:
                out.append(line)
        for k, v in remaining.items():
            out.append(f"{k}={_quote(v)}")
        _atomic_write(ENV_PATH, "\n".join(out) + "\n")


def get_setting(key):
    """Environment variable > .env > default."""
    if os.environ.get(key):
        return os.environ[key]
    return read_env_file().get(key, DEFAULTS.get(key, ""))


def env_overrides():
    """Settings currently forced by real environment variables (UI shows these)."""
    return sorted(k for k in DEFAULTS if os.environ.get(k))


# ── secrets (UniFi passwords / API keys) ──────────────────────────────────────
def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def secret_storage_kind():
    return "credential-manager" if _keyring() else "file"


def _read_secret_file():
    if not os.path.exists(SECRETS_PATH):
        return {}
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def set_secret(profile_id, secret):
    with _lock:
        kr = _keyring()
        if kr:
            kr.set_password(KEYRING_SERVICE, f"profile:{profile_id}", secret)
            return
        data = _read_secret_file()
        data[profile_id] = secret
        _atomic_write(SECRETS_PATH, json.dumps(data, indent=2))


def get_secret(profile_id):
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(KEYRING_SERVICE, f"profile:{profile_id}")
            if v is not None:
                return v
        except Exception:
            pass
    return _read_secret_file().get(profile_id)


def delete_secret(profile_id):
    with _lock:
        kr = _keyring()
        if kr:
            try:
                kr.delete_password(KEYRING_SERVICE, f"profile:{profile_id}")
            except Exception:
                pass
        data = _read_secret_file()
        if profile_id in data:
            del data[profile_id]
            _atomic_write(SECRETS_PATH, json.dumps(data, indent=2))


# ── connection profiles ───────────────────────────────────────────────────────
PROFILE_FIELDS = {
    "name":         str,   # friendly label, e.g. "Home" or "GASF Venue"
    "host":         str,   # IP or hostname of the console / controller
    "port":         int,   # 443 UniFi OS console, 8443 self-hosted, 11443 UniFi OS Server
    "site":         str,   # internal site id, usually "default"
    "console_type": str,   # "unifi_os" | "standalone" (filled in by Test)
    "auth":         str,   # "password" | "apikey"
    "username":     str,   # only for auth == "password"
    "fingerprint":  str,   # SHA-256 of the console's TLS certificate (pinned on Test)
}


def _load():
    if not os.path.exists(PROFILES_PATH):
        return {"active": None, "profiles": []}
    with open(PROFILES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active", None)
    data.setdefault("profiles", [])
    return data


def _save(data):
    _atomic_write(PROFILES_PATH, json.dumps(data, indent=2))


def clean_profile(raw):
    """Validate/normalise user input into a profile dict (no secret)."""
    p = {}
    for k, typ in PROFILE_FIELDS.items():
        if k in raw and raw[k] is not None and raw[k] != "":
            try:
                p[k] = typ(raw[k]).strip() if typ is str else typ(raw[k])
            except (TypeError, ValueError):
                raise ValueError(f"Invalid value for {k}")
    host = p.get("host", "")
    host = re.sub(r"^https?://", "", host, flags=re.I).rstrip("/")
    m = re.match(r"^\[([0-9A-Fa-f:.]+)\](?::(\d+))?$", host)   # [IPv6] or [IPv6]:port
    if m:
        host = m.group(1)
        if m.group(2) and "port" not in p:
            p["port"] = int(m.group(2))
    elif host.count(":") == 1:                                 # host:port typed together
        host, _, port = host.partition(":")
        if port.isdigit() and "port" not in p:
            p["port"] = int(port)
    p["host"] = host
    if not p["host"]:
        raise ValueError("Address is required")
    p.setdefault("port", 443)
    if not 1 <= p["port"] <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    p.setdefault("site", "default")
    p.setdefault("auth", "password")
    if p["auth"] not in ("password", "apikey"):
        raise ValueError("auth must be 'password' or 'apikey'")
    if p["auth"] == "password" and not p.get("username"):
        raise ValueError("Username is required for password login")
    if p.get("console_type") not in (None, "unifi_os", "standalone"):
        raise ValueError("Unknown console type")
    p.setdefault("name", p["host"])
    return p


def list_profiles():
    with _lock:
        data = _load()
        out = []
        for p in data["profiles"]:
            q = dict(p)
            q["has_secret"] = bool(get_secret(p["id"]))
            out.append(q)
        return {"active": data["active"], "profiles": out}


def get_profile(profile_id):
    with _lock:
        for p in _load()["profiles"]:
            if p["id"] == profile_id:
                return dict(p)
    return None


def upsert_profile(raw, secret=None):
    with _lock:
        data = _load()
        pid = raw.get("id")
        existing = next((p for p in data["profiles"] if p["id"] == pid), None) if pid else None
        merged = dict(existing or {})
        merged.update({k: v for k, v in raw.items() if k in PROFILE_FIELDS})
        profile = clean_profile(merged)
        if existing:
            # A changed address or port means a different certificate: drop the pin
            # unless the caller supplied a fresh one from a Test.
            if (existing.get("host"), existing.get("port")) != (profile["host"], profile["port"]) \
                    and "fingerprint" not in raw:
                profile.pop("fingerprint", None)
            profile["id"] = existing["id"]
            data["profiles"] = [profile if p["id"] == pid else p for p in data["profiles"]]
        else:
            profile["id"] = uuid.uuid4().hex[:12]
            data["profiles"].append(profile)
        if secret:
            set_secret(profile["id"], secret)
        elif not existing:
            raise ValueError("A password or API key is required for a new connection")
        elif existing.get("auth") != profile["auth"]:
            raise ValueError("Changing the login method needs the new password or API key")
        if data["active"] is None:
            data["active"] = profile["id"]
        _save(data)
        return profile


def update_profile_fields(profile_id, **fields):
    with _lock:
        data = _load()
        for p in data["profiles"]:
            if p["id"] == profile_id:
                p.update(fields)
                _save(data)
                return dict(p)
    return None


def delete_profile(profile_id):
    with _lock:
        data = _load()
        data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
        if data["active"] == profile_id:
            data["active"] = data["profiles"][0]["id"] if data["profiles"] else None
        _save(data)
        delete_secret(profile_id)
        return data["active"]


def set_active(profile_id):
    with _lock:
        data = _load()
        if profile_id is not None and not any(p["id"] == profile_id for p in data["profiles"]):
            raise ValueError("Unknown connection")
        data["active"] = profile_id
        _save(data)


def active_profile():
    with _lock:
        data = _load()
        if not data["active"]:
            return None
        return next((dict(p) for p in data["profiles"] if p["id"] == data["active"]), None)
