"""
UniFi x flinchbot - local web app (AI: Claude or ChatGPT).

Run:  start.bat (Windows), start.command (macOS / Linux),
      or   python backend/server.py [--port N] [--no-browser]

Security
  * Listens on 127.0.0.1 only by default (SERVER_HOST in .env to change).
  * Every /api call needs a random token that is generated at each start and
    embedded in the page this server serves, so other websites open in your
    browser cannot drive the API.
  * Host header must be localhost or an IP address (blocks DNS-rebinding).
  * No CORS headers are sent, and the page can't be framed.
"""

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_store as cfg                                     # noqa: E402
from agent import Agent, AgentError, redact                    # noqa: E402
from providers import PROVIDERS, provider_error_message      # noqa: E402
from unifi_client import (CONSOLE_LABELS, UniFiClient, UniFiError,  # noqa: E402
                          format_fingerprint, test_connection)
from ssh_client import SSHClient, SSHError, format_hostkey, test_ssh  # noqa: E402
import envcheck                                                        # noqa: E402
import history                                                         # noqa: E402
import platform                                                        # noqa: E402
import topology                                                        # noqa: E402
import wan                                                             # noqa: E402
from unifi_client import summarize_event                               # noqa: E402
from urllib.parse import parse_qs                                      # noqa: E402

APP_VERSION = "2.5.0"
APP_TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 1_000_000


# ── application state ─────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self._client = None
        self._client_profile = None
        self._ssh = None
        self._ssh_profile = None

    def invalidate(self):
        with self.lock:
            self._client = None
            self._client_profile = None
            if self._ssh is not None:
                self._ssh.close()
            self._ssh = None
            self._ssh_profile = None

    def ssh(self):
        """SSH client for the active profile (only if SSH is turned on for it)."""
        with self.lock:
            profile = cfg.active_profile()
            if not profile:
                raise SSHError("No UniFi connection yet.", "noconn")
            if not profile.get("ssh_enabled"):
                raise SSHError(f"SSH is turned off for '{profile['name']}'. Turn it on in the "
                               f"connection settings if you want this.", "disabled")
            if self._ssh is None or self._key(profile, self.SSH_KEYS) != self._ssh_profile:
                secret = cfg.get_secret(profile["id"], "ssh")
                if not secret:
                    raise SSHError(f"No SSH password is saved for '{profile['name']}'. Edit the "
                                   f"connection and enter it again.", "auth")
                self._ssh = SSHClient(profile["host"], profile.get("ssh_port"), profile.get("ssh_username"),
                                      secret, expected_hostkey=profile.get("ssh_hostkey"))
                self._ssh_profile = self._key(profile, self.SSH_KEYS)
            return self._ssh

    # Only these fields mean "a different login": changing e.g. the speed-test schedule
    # must not throw away the session (and the sign-in circuit breaker with it).
    CONN_KEYS = ("id", "host", "port", "site", "auth", "username", "console_type", "fingerprint")
    SSH_KEYS = ("id", "host", "ssh_enabled", "ssh_port", "ssh_username", "ssh_hostkey")

    @staticmethod
    def _key(profile, keys):
        return tuple(profile.get(k) for k in keys)

    def client(self):
        """UniFi client for the active profile; rebuilt if the login details changed."""
        with self.lock:
            profile = cfg.active_profile()
            if not profile:
                raise UniFiError("No UniFi connection yet. Click the connection menu at the top "
                                 "and add one.", "noconn")
            if self._client is None or self._key(profile, self.CONN_KEYS) != self._client_profile:
                secret = cfg.get_secret(profile["id"])
                if not secret:
                    raise UniFiError(f"No password or API key is saved for '{profile['name']}'. "
                                     f"Edit the connection and enter it again.", "auth")
                self._client = UniFiClient(profile, secret)
                self._client.verified = False
                self._client_profile = self._key(profile, self.CONN_KEYS)
            return self._client


state = State()


def agent_settings():
    try:
        rounds = int(cfg.get_setting("MAX_TOOL_ROUNDS"))
    except ValueError:
        rounds = 15
    try:
        out_tokens = int(cfg.get_setting("MAX_OUTPUT_TOKENS"))
    except ValueError:
        out_tokens = 16000
    prov = cfg.active_provider()
    return {"provider": prov, "provider_label": PROVIDERS[prov].label,
            "api_key": cfg.provider_key(prov), "model": cfg.provider_model(prov),
            "keys": {p: cfg.provider_key(p) for p in PROVIDERS},
            "max_rounds": max(1, min(rounds, 50)),
            # 21,000 is the SDK's ceiling for a non-streaming request
            "max_tokens": max(2000, min(out_tokens, 21000))}


def agent_context():
    p = cfg.active_profile() or {}
    return {"name": p.get("name"), "site": p.get("site", "default"),
            "console": CONSOLE_LABELS.get(p.get("console_type") or "unifi_os"),
            "ssh_enabled": bool(p.get("ssh_enabled")), "ssh_username": p.get("ssh_username")}


def wan_history(days):
    """WAN history for the active connection (the AI's get_wan_history tool and the WAN view)."""
    c, p = state.client(), cfg.active_profile()
    points, events, notes = wan.collect(c, days, history.speedtests(p["id"]))
    current = None
    try:
        current = next((h for h in c.get_health() if h.get("subsystem") == "wan"), None)
    except UniFiError as e:
        notes.append(f"Current WAN health isn't available ({e}).")
    return {"days": days, "summary": wan.summary(points), "speed_tests": points[-200:], "wan_events": events[-100:],
            "outages": sum(1 for e in events if wan.is_outage(e)), "current_wan": current, "notes": notes,
            "schedule_hours": p.get("speedtest_hours", 0)}


changelog = history.ChangeLog(state.client, cfg.active_profile)
agent = Agent(state.client, agent_settings, agent_context, get_ssh=state.ssh, changes=changelog, get_wan=wan_history)


def _last_speedtest_ms(connection_id):
    runs = [r.get("ran_at") or 0 for r in history.speedtests(connection_id) if r.get("via") == "schedule"]
    return max(runs) if runs else None


scheduler = wan.Scheduler(cfg.active_profile, state.client, _last_speedtest_ms, history.record_speedtest)


def switch_connection():
    """Call after the active connection (or its details) changed."""
    state.invalidate()
    agent.reset()


# ── helpers ───────────────────────────────────────────────────────────────────
def ai_error_message(e):
    prov = cfg.active_provider()
    return provider_error_message(e, prov, cfg.provider_model(prov))


def mask_key(k):
    return f"…{k[-4:]}" if k and len(k) > 8 else ("set" if k else "")


def public_profile(p):
    q = dict(p)
    q["fingerprint_display"] = format_fingerprint(p.get("fingerprint"))
    q["ssh_hostkey_display"] = format_hostkey(p.get("ssh_hostkey"))
    q["console_label"] = CONSOLE_LABELS.get(p.get("console_type"), "Not tested yet")
    return q


def _host_allowed(host_header, port):
    if not host_header:
        return False
    host = host_header.strip()
    m_port = None
    if host.startswith("["):                       # [ipv6]:port
        end = host.find("]")
        name, rest = host[1:end], host[end + 1:]
        if rest.startswith(":"):
            m_port = rest[1:]
    elif host.count(":") == 1:
        name, m_port = host.split(":")
    else:
        name = host
    if m_port is not None and m_port != str(port):
        return False
    name = name.lower()
    extra = {h.strip().lower() for h in cfg.get_setting("ALLOWED_HOSTS").split(",") if h.strip()} \
        if cfg.get_setting("ALLOWED_HOSTS") else set()
    if name in ("localhost",) or name in extra:
        return True
    try:
        ipaddress.ip_address(name)                # IP literals can't be DNS-rebound
        return True
    except ValueError:
        return False


class HttpError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "UniFiFlinchbot"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass   # quiet; errors are printed explicitly

    # responses
    def _headers(self, status, ctype, length, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

    STATIC_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
                    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}

    def send_static(self, name):
        """Images for the UI. Only plain file names in frontend/static, no subfolders."""
        import re
        ext = os.path.splitext(name)[1].lower()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or ext not in self.STATIC_TYPES:
            raise HttpError(404, "Not found")
        return self.send_file(os.path.join(cfg.FRONTEND_DIR, "static", name), self.STATIC_TYPES[ext], cache=True)

    def send_file(self, path, ctype, cache=False):
        if not os.path.isfile(path):
            raise HttpError(404, "Not found")
        with open(path, "rb") as f:
            body = f.read()
        self._headers(200, ctype, len(body), cache=cache)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; script-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_page(self):
        path = os.path.join(cfg.FRONTEND_DIR, "index.html")
        with open(path, encoding="utf-8") as f:
            html = f.read().replace("__APP_TOKEN__", APP_TOKEN).replace("__APP_VERSION__", APP_VERSION)
        body = html.encode()
        self._headers(200, "text/html; charset=utf-8", len(body))
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    # request gatekeeping
    def _check(self, api):
        if not _host_allowed(self.headers.get("Host"), self.server.server_port):
            raise HttpError(403, "Host not allowed")
        origin = self.headers.get("Origin")
        if origin and origin not in ("null",):
            o = origin.split("://", 1)[-1]
            if o.lower() != (self.headers.get("Host") or "").lower():
                raise HttpError(403, "Cross-origin request blocked")
        if api:
            tok = self.headers.get("X-App-Token", "")
            if not hmac.compare_digest(tok, APP_TOKEN):
                raise HttpError(401, "This page is out of date (the app was restarted). Reload the page.",
                                kind="token")

    def _body(self):
        if self.command in ("POST", "PUT", "DELETE"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Request too large")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise HttpError(400, "Invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "Expected a JSON object")
        return data

    def _dispatch(self):
        path = self.path.split("?", 1)[0]
        try:
            if self.command == "GET" and path in ("/", "/index.html"):
                self._check(api=False)
                return self.send_page()
            if self.command == "GET" and path == "/favicon.ico":
                self._check(api=False)
                return self.send_static("favicon.png")
            if self.command == "GET" and path.startswith("/static/"):
                self._check(api=False)
                return self.send_static(path[len("/static/"):])
            if self.command == "GET" and path in ("/setup", "/setup.html"):
                self._check(api=False)
                return self.send_file(os.path.join(cfg.BASE_DIR, "SETUP.html"), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                raise HttpError(404, "Not found")
            self._check(api=True)
            body = self._body()
            if self.command == "GET" and "?" in self.path:
                body = {k: v[-1] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
            route = ROUTES.get((self.command, path))
            if not route:
                raise HttpError(404, f"Unknown endpoint {self.command} {path}")
            result = route(body)
            self.send_json(result if result is not None else {"ok": True})
        except HttpError as e:
            self.send_json({"error": str(e), **e.extra}, e.status)
        except UniFiError as e:
            self.send_json({"error": str(e), "kind": e.kind}, 502)
        except SSHError as e:
            self.send_json({"error": str(e), "kind": "ssh_" + e.kind}, 502)
        except (AgentError, ValueError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            msg = ai_error_message(e)
            if msg:
                self.send_json({"error": msg, "kind": "ai"}, 502)
            else:
                traceback.print_exc()
                self.send_json({"error": f"Internal error: {type(e).__name__}: {e}"}, 500)

    do_GET = do_POST = _dispatch

    def do_OPTIONS(self):
        self.send_response(405)      # no CORS: cross-site callers are not welcome
        self.send_header("Content-Length", "0")
        self.end_headers()


# ── routes ────────────────────────────────────────────────────────────────────
def r_status(_):
    p = cfg.active_profile()
    out = {
        "version": APP_VERSION,
        "configured": bool(cfg.provider_key(cfg.active_provider())),
        "provider": cfg.active_provider(),
        "provider_label": PROVIDERS[cfg.active_provider()].label,
        "has_connections": bool(cfg.list_profiles()["profiles"]),
        "active": public_profile(p) if p else None,
        "connected": False, "error": None, "error_kind": None,
        "secret_storage": cfg.secret_storage_kind(),
        "secret_store_name": cfg.secret_store_name(),
        "env": {"os": envcheck.describe_os(), "python": platform.python_version()},
        "platform": cfg.platform_name(),
        "launcher": cfg.launcher(),
        "pending": agent.pending_view(),
        "model": cfg.provider_model(cfg.active_provider()),
        "model_warning": None,
    }
    try:
        out["model_warning"] = model_warning()
    except Exception:
        pass
    if p:
        try:
            c = state.client()
            if not c.verified:
                c.get_health()
                c.verified = True
            out["connected"] = True
        except UniFiError as e:
            out["error"], out["error_kind"] = str(e), e.kind
        try:
            b = state.client().login_blocked()
            if b:
                out["login_blocked"] = True
                out["retry_after"] = int(b["until"] - time.time()) if b["until"] else None
        except UniFiError:
            pass
    return out


def r_settings_get(_):
    providers = {}
    for name, cls in PROVIDERS.items():
        key = cfg.provider_key(name)
        providers[name] = {"label": cls.label, "vendor": cls.vendor, "key_url": cls.key_url,
                           "key_prefix": cls.key_prefix, "key_set": bool(key),
                           "key_hint": mask_key(key), "model": cfg.provider_model(name)}
    return {"provider": cfg.active_provider(), "providers": providers,
            "port": int(cfg.get_setting("SERVER_PORT")),
            "running_port": HTTP_PORT,
            "env_overrides": cfg.env_overrides(),
            "env_path": cfg.ENV_PATH}


def r_settings_save(b):
    updates = {}
    before = cfg.active_provider()
    if b.get("provider"):
        if b["provider"] not in PROVIDERS:
            raise ValueError("Unknown AI provider")
        updates["AI_PROVIDER"] = b["provider"]
    for name, k in (b.get("keys") or {}).items():
        if name not in PROVIDERS or not k:
            continue
        k = str(k).strip()
        cls = PROVIDERS[name]
        if not k.startswith(cls.key_prefix):
            raise ValueError(f"That doesn't look like an {cls.vendor} API key (they start with '{cls.key_prefix}').")
        if name == "openai" and k.startswith("sk-ant-"):
            raise ValueError("That's an Anthropic key. Paste it in the Claude section instead.")
        updates[cfg.PROVIDER_KEYS[name]["key"]] = k
    for name, m in (b.get("models") or {}).items():
        if name in PROVIDERS and m:
            updates[cfg.PROVIDER_KEYS[name]["model"]] = str(m).strip()
    if b.get("port") not in (None, ""):
        port = int(b["port"])
        if not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        updates["SERVER_PORT"] = str(port)
    if updates:
        cfg.write_env_file(updates)
    res = r_settings_get(None)
    res["restart_needed"] = "SERVER_PORT" in updates and res["port"] != HTTP_PORT
    res["chat_reset"] = cfg.active_provider() != before
    if res["chat_reset"]:
        agent.reset()            # conversations can't move between providers
    return res


def r_test_key(b):
    prov = b.get("provider") or cfg.active_provider()
    if prov not in PROVIDERS:
        return {"ok": False, "error": "Unknown AI provider"}
    try:
        models = agent.list_models(prov, str(b.get("api_key") or "").strip() or None)
        return {"ok": True, "models": models}
    except AgentError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": provider_error_message(e, prov) or str(e)}


# Model catalogue cache: lets /api/status warn about a deprecated/missing model
# without calling the provider on every 30-second poll.
_models_cache = {"key": None, "at": 0.0, "models": None, "error": None}
_models_lock = threading.Lock()
MODELS_TTL, MODELS_ERR_TTL = 6 * 3600, 300


def cached_models(force=False):
    prov = cfg.active_provider()
    key = cfg.provider_key(prov)
    if not key:
        return None
    with _models_lock:
        c = _models_cache
        ttl = MODELS_ERR_TTL if c["error"] else MODELS_TTL
        if force or c["key"] != (prov, key) or time.time() - c["at"] > ttl:
            try:
                c.update(models=agent.list_models(prov, key), error=None)
            except Exception as e:
                c.update(models=None, error=provider_error_message(e, prov) or str(e))
            c.update(key=(prov, key), at=time.time())
        return c["models"]


def model_warning():
    """Human-readable warning if the configured model is deprecated or unavailable."""
    models = cached_models()
    if not models:
        return None
    current = cfg.provider_model(cfg.active_provider())
    m = next((x for x in models if x["id"] == current), None)
    if m is None:
        return f"The model '{current}' isn't available to your API key. Pick another in Settings."
    if m["lifecycle"] != "active":
        when = f" and retires on {m['retires_at']}" if m.get("retires_at") else ""
        return f"{m['name']} is deprecated{when}. Pick a newer model in Settings."
    return None


def r_models(_):
    models = cached_models(force=True)
    if models is None:
        raise AgentError(_models_cache["error"] or "Enter an API key first.")
    return {"models": models, "current": cfg.provider_model(cfg.active_provider())}


def r_connections(_):
    data = cfg.list_profiles()
    data["profiles"] = [public_profile(p) for p in data["profiles"]]
    data["secret_storage"] = cfg.secret_storage_kind()
    return data


def r_connection_save(b):
    secret = (b.pop("secret", None) or "").strip() or None
    ssh_secret = (b.pop("ssh_secret", None) or "").strip() or None
    was_active = cfg.list_profiles()["active"]
    profile = cfg.upsert_profile(b, secret, ssh_secret)
    if b.get("activate") or not was_active or was_active == profile["id"]:
        cfg.set_active(profile["id"])
        switch_connection()
    return r_connections(None) | {"saved_id": profile["id"]}


def r_connection_test(b):
    secret = (b.pop("secret", None) or "").strip()
    stored = cfg.get_profile(b["id"]) if b.get("id") else None
    if stored and not secret:
        secret = cfg.get_secret(stored["id"]) or ""
    if not secret:
        raise ValueError("Enter the password or API key to test.")
    merged = dict(stored or {})
    merged.update({k: v for k, v in b.items() if k in cfg.PROFILE_FIELDS})
    merged.pop("console_type", None)          # always auto-detect on Test
    profile = cfg.clean_profile(merged)
    if stored:
        profile["fingerprint"] = stored.get("fingerprint")

    # Never resend credentials the console just rejected, and never contact a
    # console that just locked sign-ins: each attempt can extend UniFi's lockout.
    now = time.time()
    host_key = (profile["host"].lower(), profile["port"])
    cred_key = host_key + (profile.get("auth"), profile.get("username", ""),
                           hashlib.sha256(secret.encode()).hexdigest())
    for key in (host_key, cred_key):
        hit = _test_failures.get(key)
        if hit and now < hit["until"]:
            wait = int(hit["until"] - now)
            return {"ok": False, "kind": hit["kind"], "error": hit["error"] +
                    f" (Not sent again: {'try again in about ' + str(max(1, wait // 60)) + ' min' if hit['kind'] == 'ratelimit' else 'change the username or password first'}.)"}
    result = test_connection(profile, secret)
    if not result["ok"]:
        if result["kind"] == "ratelimit":
            _test_failures[host_key] = {"kind": "ratelimit", "error": result["error"], "until": now + 600}
        elif result["kind"] in ("auth", "mfa"):
            _test_failures[cred_key] = {"kind": result["kind"], "error": result["error"], "until": now + 3600}
    else:
        _test_failures.pop(host_key, None)
    return result


_test_failures = {}     # recent failed tests: (host,port) for lockouts, (+creds) for rejections


def r_ssh_test(b):
    """Test SSH for a connection (saved or being edited): login, host key, one command."""
    secret = (b.pop("ssh_secret", None) or "").strip()
    stored = cfg.get_profile(b["id"]) if b.get("id") else None
    if stored and not secret:
        secret = cfg.get_secret(stored["id"], "ssh") or ""
    if not secret:
        raise ValueError("Enter the SSH password to test.")
    merged = dict(stored or {})
    merged.update({k: v for k, v in b.items() if k in cfg.PROFILE_FIELDS})
    # Only the SSH side matters here; the UniFi login isn't part of an SSH test.
    host, _ = cfg.normalize_host(merged.get("host"))
    try:
        ssh_port = int(merged.get("ssh_port") or 22)
    except (TypeError, ValueError):
        raise ValueError("Invalid SSH port")
    if not 1 <= ssh_port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    profile = {"host": host, "ssh_port": ssh_port,
               "ssh_username": str(merged.get("ssh_username") or "").strip() or "root",
               "ssh_hostkey": (stored or {}).get("ssh_hostkey")}

    # Same rule as the UniFi login test: never resend a password the box just rejected.
    now = time.time()
    cred_key = ("ssh", profile["host"].lower(), profile["ssh_port"], profile["ssh_username"],
                hashlib.sha256(secret.encode()).hexdigest())
    hit = _test_failures.get(cred_key)
    if hit and now < hit["until"]:
        return {"ok": False, "kind": hit["kind"],
                "error": hit["error"] + " (Not sent again: change the username or password first.)"}
    result = test_ssh(profile, secret)
    if not result["ok"] and result["kind"] == "auth":
        _test_failures[cred_key] = {"kind": "auth", "error": result["error"], "until": now + 3600}
    return result


def r_connection_retry(_):
    """User pressed Retry: allow exactly one new sign-in attempt for the active connection."""
    c = state.client()
    c.clear_login_block()
    c.verified = False
    return r_status(None)


def r_connection_activate(b):
    cfg.set_active(b.get("id"))
    switch_connection()
    return r_status(None)


def r_connection_trust(b):
    stored = cfg.get_profile(b.get("id", ""))
    if not stored:
        raise ValueError("Unknown connection")
    res = test_connection(dict(stored, fingerprint=None), cfg.get_secret(stored["id"]) or "")
    if res.get("ok"):
        cfg.update_profile_fields(stored["id"], fingerprint=res["fingerprint"],
                                  console_type=res["console_type"])
        switch_connection()
    return res


def r_connection_delete(b):
    cfg.delete_profile(b.get("id", ""))
    switch_connection()
    return r_connections(None)


def r_network(_):
    c = state.client()
    sections, errors = {}, {}
    for key, fn in [("health", c.get_health), ("devices", c.get_devices), ("clients", c.get_clients),
                    ("alerts", c.get_alerts), ("wlans", c.get_wlans)]:
        try:
            sections[key] = fn()
        except UniFiError as e:
            if e.kind in ("unreachable", "cert", "auth", "noconn"):
                raise           # whole connection is down: report it once
            sections[key], errors[key] = [], str(e)
    c.verified = True
    devs = sections["devices"]
    return {
        "device_count": len(devs), "client_count": len(sections["clients"]),
        "alert_count": len(sections["alerts"]), "wlan_count": len(sections["wlans"]),
        "health": sections["health"], "errors": errors,
        "devices": [{"name": d.get("name") or d.get("hostname") or d.get("mac", "Unknown"),
                     "type": d.get("type", ""), "model": d.get("model", ""), "mac": d.get("mac", ""),
                     "state": d.get("state", 0), "ip": d.get("ip", ""), "version": d.get("version", ""),
                     "num_sta": d.get("num_sta", 0)} for d in devs],
        "alerts": redact(sections["alerts"][:10]),
        # for the sidebar list + "Explain": a readable title, and the redacted entry to hand to the AI
        "alert_items": [dict(summarize_event(a), raw=redact(a)) for a in sections["alerts"][:10]],
        "wlans": [{"id": w.get("_id"), "name": w.get("name"), "enabled": w.get("enabled", False)}
                  for w in sections["wlans"]],
    }


def r_topology(_):
    """Device tree + problem clients as a model and as SVG (vector, shown inline and downloadable)."""
    c = state.client()
    devices, clients = c.get_devices(), c.get_clients()
    try:
        events = c.get_events(500, 24, ["CLIENT_DEVICES"])
    except UniFiError:
        events = None                          # flapping detection is best-effort
    p = cfg.active_profile()
    model = topology.build_model(devices, clients, events, network=p["name"], site=p.get("site", "default"))
    return {"svg": topology.render_svg(model), "totals": model["totals"], "generated_at": model["generated_at"],
            "network": p["name"], "thresholds": model["thresholds"]}


def r_changes(b):
    here = (b.get("scope") or "here") == "here"
    p = cfg.active_profile()
    return {"changes": changelog.list(limit=500, connection_id=(p or {}).get("id") if here else None),
            "active": (p or {}).get("id")}


def r_change_undo(b):
    rid = str(b.get("id") or "")
    res = changelog.undo(rid, force=bool(b.get("force")))
    if res.get("ok"):
        rec = changelog.get(rid)
        agent.add_note(f"The user pressed Undo on an earlier change ({rec['label']}"
                       f"{' – ' + rec['target'] if rec.get('target') else ''}). Result: {res['detail']}")
    return res


def r_wan(b):
    try:
        days = max(1, min(int(b.get("days") or 7), 90))
    except (TypeError, ValueError):
        days = 7
    p = cfg.active_profile()
    if not p:
        raise UniFiError("No UniFi connection yet.", "noconn")
    h = wan_history(days)
    svg = wan.render_svg(h["speed_tests"], h["wan_events"], days, network=p["name"])
    return dict(h, svg=svg, network=p["name"], running=wan.is_running(), last_error=scheduler.last_error,
                schedule_choices=list(cfg.SPEEDTEST_CHOICES),
                generated_at=datetime.now().astimezone().isoformat(timespec="seconds"))


def r_wan_speedtest(_):
    """The user pressed 'Run a speed test now' (an explicit action, so no approval card); logged."""
    c, p = state.client(), cfg.active_profile()
    try:
        point = wan.run_speedtest(c)
    except UniFiError as e:
        changelog.record("speedtest", {}, "Run a WAN speed test", "approved",
                         {"content": str(e), "is_error": True}, via="button")
        raise
    history.record_speedtest(p["id"], point, "button")
    changelog.record("speedtest", {}, "Run a WAN speed test", "approved",
                     {"content": f"{point['download_mbps']} / {point['upload_mbps']} Mbps, {point['latency_ms']} ms",
                      "is_error": False}, via="button")
    return {"point": point}


def r_wan_schedule(b):
    p = cfg.active_profile()
    if not p:
        raise UniFiError("No UniFi connection yet.", "noconn")
    try:
        hours = int(b.get("hours"))
    except (TypeError, ValueError):
        raise ValueError("hours must be a number")
    if hours not in cfg.SPEEDTEST_CHOICES:
        raise ValueError("Unsupported schedule")
    cfg.update_profile_fields(p["id"], speedtest_hours=hours)
    return {"schedule_hours": hours}


_backup_lock = threading.Lock()


def r_backup(_):
    """One click: the console makes a settings backup (.unf) and the browser saves it.
    It's the file UniFi's own Settings → System → Backups → Restore accepts."""
    if not _backup_lock.acquire(blocking=False):
        raise AgentError("A backup is already being made. Wait for it to finish.")
    try:
        c, p = state.client(), cfg.active_profile()
        try:
            body, console_name = c.download_backup()
        except UniFiError as e:
            changelog.record("backup", {}, "Download a config backup", "approved",
                             {"content": str(e), "is_error": True}, via="button")
            raise
        net = re.sub(r"[^A-Za-z0-9._-]+", "-", p["name"]).strip("-") or "unifi"
        version = console_name.rsplit(".unf", 1)[0]
        filename = f"unifi-backup_{net}_{datetime.now().strftime('%Y-%m-%d_%H%M')}_v{version}.unf"
        changelog.record("backup", {}, "Download a config backup", "approved",
                         {"content": f"{len(body):,} bytes, saved by the browser as {filename}", "is_error": False},
                         via="button")
        return {"filename": filename, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "data": base64.b64encode(body).decode()}
    finally:
        _backup_lock.release()


def r_chat(b):
    return agent.chat(str(b.get("message", "")), b.get("files"))


def r_decide(b):
    return agent.decide(bool(b.get("approve")))


def r_reset(_):
    agent.reset()


_report_lock = threading.Lock()


def r_report(_):
    """Read-only, whole-network action report (separate from the chat)."""
    if not _report_lock.acquire(blocking=False):
        raise AgentError("A report is already being generated. Wait for it to finish.")
    try:
        state.client()                       # fail fast with a clear message if not connected
        p = cfg.active_profile()
        report = agent.generate_report()
        report.update(generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                      network=p["name"], host=p["host"], site=p.get("site", "default"),
                      model=f"{PROVIDERS[cfg.active_provider()].label} · {cfg.provider_model(cfg.active_provider())}",
                      report_id=f"{p['id']}-{int(time.time())}")
        return report
    finally:
        _report_lock.release()


ROUTES = {
    ("GET",  "/api/status"):                  r_status,
    ("GET",  "/api/settings"):                r_settings_get,
    ("POST", "/api/settings"):                r_settings_save,
    ("POST", "/api/settings/test-key"):       r_test_key,
    ("GET",  "/api/models"):                  r_models,
    ("GET",  "/api/connections"):             r_connections,
    ("POST", "/api/connections"):             r_connection_save,
    ("POST", "/api/connections/test"):        r_connection_test,
    ("POST", "/api/connections/ssh-test"):    r_ssh_test,
    ("POST", "/api/connections/activate"):    r_connection_activate,
    ("POST", "/api/connections/trust-cert"):  r_connection_trust,
    ("POST", "/api/connections/retry"):       r_connection_retry,
    ("POST", "/api/connections/delete"):      r_connection_delete,
    ("GET",  "/api/network"):                 r_network,
    ("GET",  "/api/topology"):                r_topology,
    ("GET",  "/api/changes"):                 r_changes,
    ("POST", "/api/changes/undo"):            r_change_undo,
    ("GET",  "/api/wan"):                     r_wan,
    ("POST", "/api/wan/speedtest"):           r_wan_speedtest,
    ("POST", "/api/wan/schedule"):            r_wan_schedule,
    ("POST", "/api/backup"):                  r_backup,
    ("POST", "/api/chat"):                    r_chat,
    ("POST", "/api/chat/decide"):             r_decide,
    ("POST", "/api/reset"):                   r_reset,
    ("POST", "/api/report"):                  r_report,
}

HTTP_PORT = None


def main():
    global HTTP_PORT
    ap = argparse.ArgumentParser(description="UniFi x flinchbot")
    ap.add_argument("--port", type=int, help="override SERVER_PORT from .env")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = ap.parse_args()

    host = cfg.get_setting("SERVER_HOST") or "127.0.0.1"
    HTTP_PORT = args.port or int(cfg.get_setting("SERVER_PORT") or 8080)
    try:
        httpd = ThreadingHTTPServer((host, HTTP_PORT), Handler)
    except OSError as e:
        print(f"\n  Could not start on {host}:{HTTP_PORT} ({e.strerror or e}).")
        print("  Another program is probably using that port. Close it, or start with")
        print(f"  a different port:   {cfg.launcher()} --port 8090\n")
        sys.exit(1)
    httpd.daemon_threads = True

    url_host = "127.0.0.1" if host in ("0.0.0.0", "::", "127.0.0.1") else host
    url = f"http://{url_host}:{HTTP_PORT}/"
    print("")
    print("  UniFi x flinchbot", APP_VERSION)
    print(f"  Open:  {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: listening on {host} - other machines on this network can reach the app.")
    p = cfg.active_profile()
    print(f"  Connection: {p['name']} ({p['host']})" if p else "  Connection: none yet - add one in the app")
    prov = cfg.active_provider()
    print(f"  AI: {PROVIDERS[prov].label} ({cfg.provider_model(prov)}), key "
          f"{'set' if cfg.provider_key(prov) else 'NOT set - add it in the app'}")
    print(f"  Secrets stored in: {cfg.secret_store_name()}")
    print("  Press Ctrl+C to stop.\n")
    scheduler.start()                  # speed tests, only for connections where a schedule is set
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
