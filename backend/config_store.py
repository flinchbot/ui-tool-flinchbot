"""
Settings (.env) and UniFi connection profiles.

Where things live (all relative to the project folder, so the whole folder
can be copied to another PC):

  .env                    app settings + AI provider API keys (never commit)
  data/connections.json   UniFi connection profiles, NO passwords
  the OS password store   UniFi / SSH passwords, API keys, undo snapshots' old secrets:
                          Windows Credential Manager or the macOS Keychain (service
                          "unifi-claude" - name kept from the app's first version so
                          saved passwords keep working)
  data/secrets.json       fallback store, used only if that password store
                          is unavailable (plaintext - the UI warns about it)

Real environment variables override .env values (standard dotenv behaviour).
"""

import json
import os
import re
import ssl
import sys
import tempfile
import threading
import uuid

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def ssl_context():
    """TLS context for downloads from the internet (GitHub, LiteLLM's price list).
    It trusts both the system's root certificates (as far as Python can read them:
    the Windows store, or OpenSSL's folder on a Mac/Linux) and certifi's bundle.
    python.org Python on a Mac has an empty OpenSSL folder until "Install
    Certificates.command" is run, so certifi is what makes downloads work there;
    keeping the system store too means a company's own root certificate (TLS
    inspection proxies) still works on Windows. The UniFi client is separate:
    it pins the console's self-signed certificate instead."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def ssl_roots():
    """What ssl_context() will trust, for the startup check.
    -> (number of root certificates loaded, certifi version or None, system cert folder or None)"""
    try:
        from importlib.metadata import version
        certifi_ver = version("certifi")
    except Exception:
        certifi_ver = None
    n = ssl_context().cert_store_stats().get("x509_ca", 0)
    # Certificates in a hashed folder (Linux, Homebrew) are read only when needed, so they
    # don't show in the count: report the folder instead if it holds any.
    folder = None
    capath = ssl.get_default_verify_paths().capath
    try:
        if capath and any(os.scandir(capath)):
            folder = capath
    except OSError:
        pass
    return n, certifi_ver, folder


def platform_name():
    return "windows" if IS_WINDOWS else "mac" if IS_MAC else "linux"


def launcher():
    """What the user double-clicks / runs to start the app on this OS."""
    return "start.bat" if IS_WINDOWS else "./start.command"

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
    "LOCAL_AI_URL":      "",                   # e.g. http://office-pc:11434/v1 (Ollama), http://localhost:1234/v1 (LM Studio)
    "LOCAL_AI_KEY":      "",                   # most local servers need none
    "LOCAL_AI_MODEL":    "",
    "LOCAL_AI_CONTEXT":  "32768",              # tokens the server reads per request; must match the server's own setting
    "SERVER_HOST":       "127.0.0.1",
    "SERVER_PORT":       "8080",
    "MAX_TOOL_ROUNDS":   "15",
    "MAX_OUTPUT_TOKENS": "16000",
}

# Keys the settings UI is allowed to write. Anything else in .env is preserved.
EDITABLE_KEYS = {"AI_PROVIDER", "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "SERVER_PORT",
                 "LOCAL_AI_URL", "LOCAL_AI_KEY", "LOCAL_AI_MODEL", "LOCAL_AI_CONTEXT"}

# Per-provider .env keys
PROVIDER_KEYS = {
    "anthropic": {"key": "ANTHROPIC_API_KEY", "model": "CLAUDE_MODEL"},
    "openai":    {"key": "OPENAI_API_KEY",    "model": "OPENAI_MODEL"},
    "local":     {"key": "LOCAL_AI_KEY",      "model": "LOCAL_AI_MODEL"},
}


def active_provider():
    p = (get_setting("AI_PROVIDER") or "anthropic").strip().lower()
    return p if p in PROVIDER_KEYS else "anthropic"


def provider_key(provider):
    return get_setting(PROVIDER_KEYS[provider]["key"])


def provider_model(provider):
    return get_setting(PROVIDER_KEYS[provider]["model"])


def local_context():
    try:
        return max(2048, min(int(get_setting("LOCAL_AI_CONTEXT") or 32768), 2_000_000))
    except ValueError:
        return 32768


def provider_ready(provider):
    """Can this AI answer? Cloud AIs need a key; Local AI needs an address and a model (a key is optional)."""
    if provider == "local":
        return bool(get_setting("LOCAL_AI_URL") and get_setting("LOCAL_AI_MODEL"))
    return bool(provider_key(provider))

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
            lines = ["# UniFi x flinchbot settings - created by the Settings screen.",
                     "# Keep this file private: it contains your AI provider API key(s)."]
        for k, v in updates.items():
            if any(c in str(v) for c in "\r\n"):
                raise ValueError(f"{k} can't contain line breaks")
        done, out = set(), []
        for line in lines:
            m = _ENV_LINE.match(line)
            key = m.group(1) if m else None
            if key in updates:
                if key in done:
                    continue        # a duplicate line would otherwise win (last one read)
                done.add(key)
                out.append(f"{key}={_quote(updates[key])}")
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in done:
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


def secret_store_name():
    """Human name of where passwords are kept, for the UI and the console banner."""
    if not _keyring():
        return "data/secrets.json (plaintext)"
    return "Windows Credential Manager" if IS_WINDOWS else "the macOS Keychain" if IS_MAC else "the system keyring"


def _read_secret_file():
    if not os.path.exists(SECRETS_PATH):
        return {}
    with open(SECRETS_PATH, encoding="utf-8") as f:
        return json.load(f)


SECRET_KINDS = ("", "ssh")      # "" = UniFi password / API key, "ssh" = SSH password


def _secret_key(profile_id, kind):
    return f"{profile_id}:{kind}" if kind else profile_id


def set_secret(profile_id, secret, kind=""):
    with _lock:
        kr = _keyring()
        if kr:
            kr.set_password(KEYRING_SERVICE, "profile:" + _secret_key(profile_id, kind), secret)
            return
        data = _read_secret_file()
        data[_secret_key(profile_id, kind)] = secret
        _atomic_write(SECRETS_PATH, json.dumps(data, indent=2))


def get_secret(profile_id, kind=""):
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(KEYRING_SERVICE, "profile:" + _secret_key(profile_id, kind))
            if v is not None:
                return v
        except Exception:
            pass
    return _read_secret_file().get(_secret_key(profile_id, kind))


def delete_secret(profile_id, kind=None):
    """Delete one kind of secret, or all of them for the profile (kind=None)."""
    kinds = SECRET_KINDS if kind is None else (kind,)
    with _lock:
        kr = _keyring()
        data = _read_secret_file()
        changed = False
        for k in kinds:
            if kr:
                try:
                    kr.delete_password(KEYRING_SERVICE, "profile:" + _secret_key(profile_id, k))
                except Exception:
                    pass
            if _secret_key(profile_id, k) in data:
                del data[_secret_key(profile_id, k)]
                changed = True
        if changed:
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
    # SSH to the console host (optional; off by default; password stored like the UniFi secret)
    "ssh_enabled":  bool,
    "ssh_port":     int,   # 22
    "ssh_username": str,   # "root" on UniFi OS consoles
    "ssh_hostkey":  str,   # SHA-256 of the SSH host key (pinned on Test SSH)
    "speedtest_hours": int,  # run a WAN speed test every N hours while this is the active connection (0 = off)
}
SPEEDTEST_CHOICES = (0, 1, 2, 3, 4, 6, 8, 12, 24, 48, 168)


def _to_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"not a boolean: {v!r}")


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


def normalize_host(host):
    """'https://192.168.1.1:8443/' -> ('192.168.1.1', 8443); '[fd00::1]' -> ('fd00::1', None)."""
    host = re.sub(r"^https?://", "", str(host or "").strip(), flags=re.I).rstrip("/")
    port = None
    m = re.match(r"^\[([0-9A-Fa-f:.]+)\](?::(\d+))?$", host)   # [IPv6] or [IPv6]:port
    if m:
        host = m.group(1)
        port = int(m.group(2)) if m.group(2) else None
    elif host.count(":") == 1:                                 # host:port typed together
        host, _, p = host.partition(":")
        port = int(p) if p.isdigit() else None
    if not host:
        raise ValueError("Address is required")
    if re.search(r"[\s/?#@\\]", host):
        raise ValueError("The address should be just a host name or IP, without a path")
    return host, port


def clean_profile(raw):
    """Validate/normalise user input into a profile dict (no secret)."""
    p = {}
    for k, typ in PROFILE_FIELDS.items():
        if k in raw and raw[k] is not None and (raw[k] != "" or typ is bool):
            try:
                p[k] = typ(raw[k]).strip() if typ is str else (_to_bool(raw[k]) if typ is bool else typ(raw[k]))
            except (TypeError, ValueError):
                raise ValueError(f"Invalid value for {k}")
    p["host"], typed_port = normalize_host(p.get("host", ""))
    if typed_port and "port" not in p:
        p["port"] = typed_port
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
    p.setdefault("ssh_enabled", False)
    p.setdefault("ssh_port", 22)
    if not 1 <= p["ssh_port"] <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    if p["ssh_enabled"] and not p.get("ssh_username"):
        p["ssh_username"] = "root"
    p.setdefault("speedtest_hours", 0)
    if p["speedtest_hours"] not in SPEEDTEST_CHOICES:
        raise ValueError(f"Speed test schedule must be one of {', '.join(map(str, SPEEDTEST_CHOICES))} hours")
    return p


def list_profiles():
    with _lock:
        data = _load()
        out = []
        for p in data["profiles"]:
            q = dict(p)
            q["has_secret"] = bool(get_secret(p["id"]))
            q["has_ssh_secret"] = bool(get_secret(p["id"], "ssh"))
            out.append(q)
        return {"active": data["active"], "profiles": out}


def get_profile(profile_id):
    with _lock:
        for p in _load()["profiles"]:
            if p["id"] == profile_id:
                return dict(p)
    return None


def upsert_profile(raw, secret=None, ssh_secret=None):
    with _lock:
        data = _load()
        pid = raw.get("id")
        existing = next((p for p in data["profiles"] if p["id"] == pid), None) if pid else None
        merged = dict(existing or {})
        merged.update({k: v for k, v in raw.items() if k in PROFILE_FIELDS})
        profile = clean_profile(merged)
        if existing:
            # A changed address or port means a different certificate / host key: drop
            # the pin unless the caller supplied a fresh one from a Test.
            if (existing.get("host"), existing.get("port")) != (profile["host"], profile["port"]) \
                    and "fingerprint" not in raw:
                profile.pop("fingerprint", None)
            if (existing.get("host"), existing.get("ssh_port")) != (profile["host"], profile["ssh_port"]) \
                    and "ssh_hostkey" not in raw:
                profile.pop("ssh_hostkey", None)
            profile["id"] = existing["id"]
        else:
            profile["id"] = uuid.uuid4().hex[:12]
        if not secret and not existing:
            raise ValueError("A password or API key is required for a new connection")
        if not secret and existing and existing.get("auth") != profile["auth"]:
            raise ValueError("Changing the login method needs the new password or API key")
        if profile["ssh_enabled"] and not ssh_secret and not get_secret(profile["id"], "ssh"):
            raise ValueError("Enter the SSH password to turn SSH on")
        if existing:
            data["profiles"] = [profile if p["id"] == pid else p for p in data["profiles"]]
        else:
            data["profiles"].append(profile)
        if secret:
            set_secret(profile["id"], secret)
        if ssh_secret:
            set_secret(profile["id"], ssh_secret, "ssh")
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
