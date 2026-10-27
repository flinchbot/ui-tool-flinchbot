"""
UniFi x Claude - local web app.

Run:  start.bat   (Windows)   or   python backend/server.py [--port N] [--no-browser]

Security
  * Listens on 127.0.0.1 only by default (SERVER_HOST in .env to change).
  * Every /api call needs a random token that is generated at each start and
    embedded in the page this server serves, so other websites open in your
    browser cannot drive the API.
  * Host header must be localhost or an IP address (blocks DNS-rebinding).
  * No CORS headers are sent, and the page can't be framed.
"""

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_store as cfg                                     # noqa: E402
from agent import Agent, AgentError, redact                    # noqa: E402
from unifi_client import (CONSOLE_LABELS, UniFiClient, UniFiError,  # noqa: E402
                          format_fingerprint, test_connection)

APP_VERSION = "2.0.0"
APP_TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 1_000_000


# ── application state ─────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self._client = None
        self._client_profile = None

    def invalidate(self):
        with self.lock:
            self._client = None
            self._client_profile = None

    def client(self):
        """UniFi client for the active profile; rebuilt if the profile changed."""
        with self.lock:
            profile = cfg.active_profile()
            if not profile:
                raise UniFiError("No UniFi connection yet. Click the connection menu at the top "
                                 "and add one.", "noconn")
            if self._client is None or profile != self._client_profile:
                secret = cfg.get_secret(profile["id"])
                if not secret:
                    raise UniFiError(f"No password or API key is saved for '{profile['name']}'. "
                                     f"Edit the connection and enter it again.", "auth")
                self._client = UniFiClient(profile, secret)
                self._client.verified = False
                self._client_profile = profile
            return self._client


state = State()


def agent_settings():
    try:
        rounds = int(cfg.get_setting("MAX_TOOL_ROUNDS"))
    except ValueError:
        rounds = 15
    return {"api_key": cfg.get_setting("ANTHROPIC_API_KEY"),
            "model": cfg.get_setting("CLAUDE_MODEL"),
            "max_rounds": max(1, min(rounds, 50))}


def agent_context():
    p = cfg.active_profile() or {}
    return {"name": p.get("name"), "site": p.get("site", "default"),
            "console": CONSOLE_LABELS.get(p.get("console_type") or "unifi_os")}


agent = Agent(state.client, agent_settings, agent_context)


def switch_connection():
    """Call after the active connection (or its details) changed."""
    state.invalidate()
    agent.reset()


# ── helpers ───────────────────────────────────────────────────────────────────
def anthropic_error_message(e):
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic rejected the API key. Open Settings and paste a valid key."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "This Anthropic key isn't allowed to do that (check the key's workspace/permissions)."
    if isinstance(e, anthropic.NotFoundError):
        return (f"Model '{cfg.get_setting('CLAUDE_MODEL')}' wasn't found for this key. "
                f"Pick another model in Settings.")
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic rate limit reached. Wait a minute and try again."
    if isinstance(e, anthropic.APIConnectionError):
        return "Can't reach api.anthropic.com. Check this PC's internet connection."
    if isinstance(e, anthropic.APIStatusError):
        msg = getattr(e, "message", str(e))
        if "credit balance" in str(msg).lower():
            return "Your Anthropic account is out of credits. Add credits at console.anthropic.com."
        return f"Anthropic API error {e.status_code}: {msg}"
    return None


def mask_key(k):
    return f"…{k[-4:]}" if k and len(k) > 8 else ("set" if k else "")


def public_profile(p):
    q = dict(p)
    q["fingerprint_display"] = format_fingerprint(p.get("fingerprint"))
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
    server_version = "UniFiClaude"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass   # quiet; errors are printed explicitly

    # responses
    def _headers(self, status, ctype, length):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
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

    def send_page(self):
        path = os.path.join(cfg.FRONTEND_DIR, "index.html")
        with open(path, encoding="utf-8") as f:
            html = f.read().replace("__APP_TOKEN__", APP_TOKEN)
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
                self.send_response(204)
                self.end_headers()
                return
            if not path.startswith("/api/"):
                raise HttpError(404, "Not found")
            self._check(api=True)
            body = self._body()
            route = ROUTES.get((self.command, path))
            if not route:
                raise HttpError(404, f"Unknown endpoint {self.command} {path}")
            result = route(body)
            self.send_json(result if result is not None else {"ok": True})
        except HttpError as e:
            self.send_json({"error": str(e), **e.extra}, e.status)
        except UniFiError as e:
            self.send_json({"error": str(e), "kind": e.kind}, 502)
        except (AgentError, ValueError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            msg = anthropic_error_message(e)
            if msg:
                self.send_json({"error": msg, "kind": "anthropic"}, 502)
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
        "configured": bool(cfg.get_setting("ANTHROPIC_API_KEY")),
        "has_connections": bool(cfg.list_profiles()["profiles"]),
        "active": public_profile(p) if p else None,
        "connected": False, "error": None, "error_kind": None,
        "secret_storage": cfg.secret_storage_kind(),
        "pending": agent.pending_view(),
        "model": cfg.get_setting("CLAUDE_MODEL"),
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
    return out


def r_settings_get(_):
    key = cfg.get_setting("ANTHROPIC_API_KEY")
    return {"anthropic_key_set": bool(key), "anthropic_key_hint": mask_key(key),
            "model": cfg.get_setting("CLAUDE_MODEL"),
            "port": int(cfg.get_setting("SERVER_PORT")),
            "running_port": HTTP_PORT,
            "env_overrides": cfg.env_overrides(),
            "env_path": cfg.ENV_PATH}


def r_settings_save(b):
    updates = {}
    if b.get("anthropic_api_key"):
        k = str(b["anthropic_api_key"]).strip()
        if not k.startswith("sk-ant-"):
            raise ValueError("That doesn't look like an Anthropic API key (they start with 'sk-ant-').")
        updates["ANTHROPIC_API_KEY"] = k
    if b.get("model"):
        updates["CLAUDE_MODEL"] = str(b["model"]).strip()
    if b.get("port") not in (None, ""):
        port = int(b["port"])
        if not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        updates["SERVER_PORT"] = str(port)
    if updates:
        cfg.write_env_file(updates)
    res = r_settings_get(None)
    res["restart_needed"] = res["port"] != HTTP_PORT
    return res


def r_test_anthropic(b):
    try:
        models = agent.list_models(str(b.get("api_key") or "").strip() or None)
        return {"ok": True, "models": models}
    except AgentError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": anthropic_error_message(e) or str(e)}


# Model catalogue cache: lets /api/status warn about a deprecated/missing model
# without calling Anthropic on every 30-second poll.
_models_cache = {"key": None, "at": 0.0, "models": None, "error": None}
_models_lock = threading.Lock()
MODELS_TTL, MODELS_ERR_TTL = 6 * 3600, 300


def cached_models(force=False):
    key = cfg.get_setting("ANTHROPIC_API_KEY")
    if not key:
        return None
    with _models_lock:
        c = _models_cache
        ttl = MODELS_ERR_TTL if c["error"] else MODELS_TTL
        if force or c["key"] != key or time.time() - c["at"] > ttl:
            try:
                c.update(models=agent.list_models(key), error=None)
            except Exception as e:
                c.update(models=None, error=anthropic_error_message(e) or str(e))
            c.update(key=key, at=time.time())
        return c["models"]


def model_warning():
    """Human-readable warning if the configured model is deprecated or unavailable."""
    models = cached_models()
    if not models:
        return None
    current = cfg.get_setting("CLAUDE_MODEL")
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
        raise AgentError(_models_cache["error"] or "Enter an Anthropic API key first.")
    return {"models": models, "current": cfg.get_setting("CLAUDE_MODEL")}


def r_connections(_):
    data = cfg.list_profiles()
    data["profiles"] = [public_profile(p) for p in data["profiles"]]
    data["secret_storage"] = cfg.secret_storage_kind()
    return data


def r_connection_save(b):
    secret = (b.pop("secret", None) or "").strip() or None
    was_active = cfg.list_profiles()["active"]
    profile = cfg.upsert_profile(b, secret)
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
    return test_connection(profile, secret)


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
        "wlans": [{"id": w.get("_id"), "name": w.get("name"), "enabled": w.get("enabled", False)}
                  for w in sections["wlans"]],
    }


def r_chat(b):
    return agent.chat(str(b.get("message", "")))


def r_decide(b):
    return agent.decide(bool(b.get("approve")))


def r_reset(_):
    agent.reset()


ROUTES = {
    ("GET",  "/api/status"):                  r_status,
    ("GET",  "/api/settings"):                r_settings_get,
    ("POST", "/api/settings"):                r_settings_save,
    ("POST", "/api/settings/test-anthropic"): r_test_anthropic,
    ("GET",  "/api/models"):                  r_models,
    ("GET",  "/api/connections"):             r_connections,
    ("POST", "/api/connections"):             r_connection_save,
    ("POST", "/api/connections/test"):        r_connection_test,
    ("POST", "/api/connections/activate"):    r_connection_activate,
    ("POST", "/api/connections/trust-cert"):  r_connection_trust,
    ("POST", "/api/connections/delete"):      r_connection_delete,
    ("GET",  "/api/network"):                 r_network,
    ("POST", "/api/chat"):                    r_chat,
    ("POST", "/api/chat/decide"):             r_decide,
    ("POST", "/api/reset"):                   r_reset,
}

HTTP_PORT = None


def main():
    global HTTP_PORT
    ap = argparse.ArgumentParser(description="UniFi x Claude")
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
        print("  a different port:   start.bat --port 8090\n")
        sys.exit(1)
    httpd.daemon_threads = True

    url_host = "127.0.0.1" if host in ("0.0.0.0", "::", "127.0.0.1") else host
    url = f"http://{url_host}:{HTTP_PORT}/"
    print("")
    print("  UniFi x Claude", APP_VERSION)
    print(f"  Open:  {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: listening on {host} - other machines on this network can reach the app.")
    p = cfg.active_profile()
    print(f"  Connection: {p['name']} ({p['host']})" if p else "  Connection: none yet - add one in the app")
    print(f"  Anthropic key: {'set' if cfg.get_setting('ANTHROPIC_API_KEY') else 'NOT set - add it in the app'}")
    print(f"  Secrets stored in: {'Windows Credential Manager' if cfg.secret_storage_kind() == 'credential-manager' else 'data/secrets.json (plaintext)'}")
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
