"""
UniFi Network API client.

Supports:
  * UniFi OS consoles (UDM/UDR/UCG/Cloud Key Gen2+/UniFi OS Server):
      login  POST /api/auth/login, API under /proxy/network
  * Self-hosted UniFi Network Application (Windows/Linux/Docker, usually :8443):
      login  POST /api/login,      API at the root
  * Login with a local username/password, or a UniFi API key (X-API-KEY header).

TLS: UniFi consoles use self-signed certificates, so normal CA validation is off.
Instead the certificate's SHA-256 fingerprint is pinned the first time you press
"Test" and every later connection must present the same certificate. That stops
you silently talking to the wrong box (or a man-in-the-middle) after moving to a
different network where the same IP belongs to something else.
"""

import functools
import hashlib
import http.client
import http.cookiejar
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request


class UniFiError(Exception):
    """User-facing error. `kind` lets the UI react (auth, unreachable, cert, ...)."""

    def __init__(self, message, kind="http", status=None, fingerprint=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.fingerprint = fingerprint


class _CertMismatch(OSError):   # OSError so urllib wraps it in URLError
    def __init__(self, seen):
        super().__init__("certificate fingerprint mismatch")
        self.seen = seen


def format_fingerprint(fp):
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper() if fp else ""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, expected_fp=None, seen=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._expected_fp = expected_fp
        self._seen = seen

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        fp = hashlib.sha256(der).hexdigest() if der else ""
        if self._seen is not None:
            self._seen.append(fp)
        if self._expected_fp and fp != self._expected_fp:
            self.sock.close()
            raise _CertMismatch(fp)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context, expected_fp, seen):
        super().__init__(context=context)
        self._ctx = context
        self._expected_fp = expected_fp
        self._seen = seen

    def https_open(self, req):
        conn = functools.partial(_PinnedHTTPSConnection,
                                 expected_fp=self._expected_fp, seen=self._seen)
        return self.do_open(conn, req, context=self._ctx)


CONSOLE_LABELS = {
    "unifi_os":   "UniFi OS console",
    "standalone": "Self-hosted Network Application",
}


class UniFiClient:
    def __init__(self, profile, secret, timeout=15):
        self.profile      = profile
        self.secret       = secret or ""
        self.auth         = profile.get("auth", "password")
        self.username     = profile.get("username", "")
        self.console_type = profile.get("console_type") or "unifi_os"
        self.site         = profile.get("site") or "default"
        self.timeout      = timeout
        host = profile["host"]
        host_fmt = f"[{host}]" if ":" in host else host
        self.base_url = f"https://{host_fmt}:{profile.get('port', 443)}"
        self.expected_fp = profile.get("fingerprint") or None
        self.seen_fps = []

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE       # self-signed; integrity comes from the pin
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _PinnedHTTPSHandler(ctx, self.expected_fp, self.seen_fps),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
        )
        self.csrf_token = None
        self.logged_in  = False
        self._routes = {}                    # endpoint variant that worked, per feature
        self._lock = threading.RLock()

    # ── plumbing ──────────────────────────────────────────────────────────────
    @property
    def prefix(self):
        return "/proxy/network" if self.console_type == "unifi_os" else ""

    def api(self, path):
        return f"{self.prefix}/api/s/{self.site}/{path}"

    def v2(self, path):
        return f"{self.prefix}/v2/api/site/{self.site}/{path}"

    @property
    def last_fingerprint(self):
        return self.seen_fps[-1] if self.seen_fps else None

    def _raw(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.csrf_token:
            headers["X-Csrf-Token"] = self.csrf_token
        if self.auth == "apikey":
            headers["X-API-KEY"] = self.secret
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                csrf = (resp.headers.get("X-Updated-Csrf-Token")
                        or resp.headers.get("X-Csrf-Token"))
                if csrf:
                    self.csrf_token = csrf
                raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except ValueError:
                    raise UniFiError(
                        f"{path} returned something that isn't JSON. Is "
                        f"{self.base_url} really a UniFi console?", "notfound")
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as e:
            raise self._network_error(e.reason)
        except (socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            raise self._network_error(e)

    def _network_error(self, reason):
        if isinstance(reason, _CertMismatch):
            return UniFiError(
                "The console at this address presented a DIFFERENT security certificate "
                "than the one saved for this connection. Either the console was reset/"
                "replaced, or this address now belongs to a different device. Open "
                "Connections and use 'Trust new certificate' only if you're sure.",
                "cert", fingerprint=reason.seen)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return UniFiError(
                f"Timed out reaching {self.base_url}. Is this PC on that network (or "
                f"its VPN)? Is the address and port right?", "unreachable")
        if isinstance(reason, ConnectionRefusedError):
            return UniFiError(
                f"{self.base_url} refused the connection. The address is reachable but "
                f"nothing is listening on port {self.profile.get('port')}. UniFi OS "
                f"consoles use 443, self-hosted controllers 8443, UniFi OS Server 11443.",
                "unreachable")
        if isinstance(reason, socket.gaierror):
            return UniFiError(f"Can't resolve the name '{self.profile['host']}'. "
                              f"Try the IP address instead.", "unreachable")
        if isinstance(reason, ssl.SSLError):
            return UniFiError(f"TLS handshake with {self.base_url} failed ({reason}). "
                              f"Make sure the port is the HTTPS port.", "unreachable")
        return UniFiError(f"Can't reach {self.base_url}: {reason}", "unreachable")

    @staticmethod
    def _http_detail(e):
        try:
            body = e.read().decode(errors="replace")
            j = json.loads(body)
            return (j.get("meta") or {}).get("msg") or j.get("message") or j.get("error") or body[:200]
        except Exception:
            return ""

    def _http_error(self, e, path):
        detail = self._http_detail(e)
        if e.code == 401:
            if self.auth == "apikey":
                return UniFiError(
                    "The console rejected the API key for this request. Some UniFi "
                    "versions only accept API keys on the official Integration API, "
                    "not the endpoints this app uses - try a local username/password.",
                    "auth", 401)
            return UniFiError("Not logged in (session expired or login rejected).", "auth", 401)
        if e.code == 403:
            return UniFiError(
                "Permission denied. The UniFi account is probably read-only (Viewer); "
                "give it Full Management / Site Admin for changes." +
                (f" ({detail})" if detail else ""), "forbidden", 403)
        if e.code == 404:
            return UniFiError(f"Not found on this console: {path}" +
                              (f" ({detail})" if detail else ""), "notfound", 404)
        if e.code == 429:
            return UniFiError("UniFi is rate-limiting requests (too many attempts). "
                              "Wait a few minutes and try again.", "ratelimit", 429)
        return UniFiError(f"UniFi returned HTTP {e.code} for {path}" +
                          (f": {detail}" if detail else ""), "http", e.code)

    # ── auth ──────────────────────────────────────────────────────────────────
    def login(self):
        """Password login. API-key profiles have nothing to log in to."""
        if self.auth == "apikey":
            self.logged_in = True
            return True
        with self._lock:
            self.cookie_jar.clear()
            self.csrf_token = None
            self.logged_in = False
            if self.console_type == "unifi_os":
                path, payload = "/api/auth/login", {
                    "username": self.username, "password": self.secret, "rememberMe": False}
            else:
                path, payload = "/api/login", {
                    "username": self.username, "password": self.secret, "remember": False}
            try:
                self._raw("POST", path, payload)
            except urllib.error.HTTPError as e:
                detail = self._http_detail(e)
                if e.code == 499 or "mfa" in detail.lower():
                    # UniFi OS answers 499 when the login is a UI.com cloud (SSO)
                    # account with two-factor sign-in. Apps can't supply an MFA code.
                    raise UniFiError(
                        "This login is a UI.com cloud account with two-factor sign-in "
                        "(UniFi asked for an MFA code). Apps can't use those. On the console "
                        "create a separate admin under Admins & Users with 'Restrict to local "
                        "access only' ticked, and use that username and password here "
                        "(see SETUP.md step 3).", "mfa", e.code)
                if e.code in (400, 401, 403):
                    raise UniFiError(
                        "Login rejected. Check the username and password. The account "
                        "must be a LOCAL admin on the console (UniFi cloud/UI.com accounts "
                        "with two-factor sign-in can't be used here).", "auth", e.code)
                if e.code == 429:
                    raise UniFiError("Too many login attempts - UniFi has paused logins "
                                     "for a few minutes. Wait, then try again.", "ratelimit", 429)
                if e.code in (404, 405):
                    raise UniFiError("This doesn't look like a "
                                     f"{CONSOLE_LABELS[self.console_type]}.", "notfound", e.code)
                raise self._http_error(e, path)
            self.logged_in = True
            return True

    def request(self, method, path, data=None, _retry=True):
        with self._lock:
            if not self.logged_in:
                self.login()
            try:
                return self._raw(method, path, data)
            except urllib.error.HTTPError as e:
                # Session expired or console rebooted: log in again once and retry.
                if _retry and self.auth == "password" and e.code in (401, 403):
                    self.logged_in = False
                    self.login()
                    return self.request(method, path, data, _retry=False)
                raise self._http_error(e, path)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, data):
        return self.request("POST", path, data)

    def put(self, path, data):
        return self.request("PUT", path, data)

    def delete(self, path):
        return self.request("DELETE", path)

    def _data(self, resp):
        return resp.get("data", []) if isinstance(resp, dict) else resp

    def _first_ok(self, candidates, cache_key=None):
        """Try (method, path, body) candidates in order; endpoints move between
        Network versions. 400 (api.err.InvalidObject = removed object), 404 and
        405 mean "not on this version". The winner is remembered per client so
        later calls go straight to it."""
        remembered = self._routes.get(cache_key) if cache_key else None
        if remembered is not None:
            candidates = [candidates[remembered]] + [c for i, c in enumerate(candidates) if i != remembered]
        last = None
        for method, path, body in candidates:
            try:
                result = self.request(method, path, body)
                if cache_key:
                    self._routes[cache_key] = next(i for i, c in enumerate(self._route_defs(cache_key))
                                                   if c[1] == path and c[0] == method)
                return result
            except UniFiError as e:
                if e.status in (400, 404, 405):
                    last = e
                    continue
                raise
        if last:
            raise last
        return {}

    def _route_defs(self, key):
        if key == "alerts":
            return [
                # UniFi Network 9.x/10.x: alerts are the "Critical" system log (v2 API).
                # Verified on Network 10.6.109, where rest/alarm -> 400 InvalidObject.
                ("POST", self.v2("system-log/critical"), {"pageNumber": 0, "pageSize": 100}),
                ("GET",  self.api("rest/alarm?archived=false"), None),   # 7.x/8.x
                ("GET",  self.api("stat/alarm"), None),                  # older still
            ]
        raise KeyError(key)

    # ── discovery ─────────────────────────────────────────────────────────────
    def list_sites(self):
        sites = self._data(self.get(f"{self.prefix}/api/self/sites"))
        return [{"name": s.get("name"), "desc": s.get("desc") or s.get("name")}
                for s in sites if isinstance(s, dict)]

    # ── READ ──────────────────────────────────────────────────────────────────
    def get_devices(self):         return self._data(self.get(self.api("stat/device")))
    def get_clients(self):         return self._data(self.get(self.api("stat/sta")))
    def get_health(self):          return self._data(self.get(self.api("stat/health")))
    def get_wlans(self):           return self._data(self.get(self.api("rest/wlanconf")))
    def get_networks(self):        return self._data(self.get(self.api("rest/networkconf")))
    def get_firewall_rules(self):  return self._data(self.get(self.api("rest/firewallrule")))
    def get_firewall_groups(self): return self._data(self.get(self.api("rest/firewallgroup")))
    def get_port_forwards(self):   return self._data(self.get(self.api("rest/portforward")))
    def get_system_info(self):     return self._data(self.get(self.api("stat/sysinfo")))
    def get_rogue_aps(self):       return self._data(self.get(self.api("stat/rogueap")))
    def get_dpi_stats(self):       return self._data(self.get(self.api("stat/stadpi")))
    def get_routing(self):         return self._data(self.get(self.api("rest/routing")))
    def get_port_profiles(self):   return self._data(self.get(self.api("rest/portconf")))
    def get_device(self, mac):     return self._data(self.get(self.api(f"stat/device/{mac.lower()}")))
    def get_client(self, mac):     return self._data(self.get(self.api(f"stat/user/{mac.lower()}")))

    def get_alerts(self):
        """Active alerts. Newer firmware: critical system-log entries; older: alarms."""
        data = self._data(self._first_ok(self._route_defs("alerts"), "alerts"))
        return data if isinstance(data, list) else []

    def get_events(self, limit=50, within_hours=720, categories=None):
        """System log (v2 API). Legacy stat/event was removed in Network 9.x+."""
        limit = max(1, min(int(limit or 50), 500))
        payload = {"pageNumber": 0, "pageSize": limit}
        if categories:
            payload["categories"] = categories
        now_ms = int(time.time() * 1000)
        timed = dict(payload, timestampFrom=now_ms - int(within_hours) * 3600 * 1000,
                     timestampTo=now_ms)
        path = self.v2("system-log/all")
        try:
            data = self.post(path, timed)
        except UniFiError as e:
            if e.status not in (400, 422):
                raise
            data = self.post(path, payload)   # some builds reject the time window
        return data.get("data", []) if isinstance(data, dict) else data

    # ── DEVICE WRITE ──────────────────────────────────────────────────────────
    def _devmgr(self, cmd, mac=None, **extra):
        body = {"cmd": cmd, **extra}
        if mac:
            body["mac"] = mac.lower()
        return self.post(self.api("cmd/devmgr"), body)

    def restart_device(self, mac):          return self._devmgr("restart", mac)
    def provision_device(self, mac):        return self._devmgr("force-provision", mac)
    def upgrade_device(self, mac):          return self._devmgr("upgrade", mac)
    def adopt_device(self, mac):            return self._devmgr("adopt", mac)
    def locate_device(self, mac, enable=True):
        return self._devmgr("set-locate" if enable else "unset-locate", mac)
    def update_device(self, device_id, settings):
        return self.put(self.api(f"rest/device/{device_id}"), settings)

    # ── CLIENT WRITE ──────────────────────────────────────────────────────────
    def _stamgr(self, cmd, mac):
        return self.post(self.api("cmd/stamgr"), {"cmd": cmd, "mac": mac.lower()})

    def block_client(self, mac):       return self._stamgr("block-sta", mac)
    def unblock_client(self, mac):     return self._stamgr("unblock-sta", mac)
    def disconnect_client(self, mac):  return self._stamgr("kick-sta", mac)
    def update_client(self, user_id, settings):
        return self.put(self.api(f"rest/user/{user_id}"), settings)

    # ── WIFI WRITE ────────────────────────────────────────────────────────────
    def update_wlan(self, wlan_id, settings):
        return self.put(self.api(f"rest/wlanconf/{wlan_id}"), settings)

    def enable_wlan(self, wlan_id):            return self.update_wlan(wlan_id, {"enabled": True})
    def disable_wlan(self, wlan_id):           return self.update_wlan(wlan_id, {"enabled": False})
    def set_wlan_password(self, wlan_id, pw):  return self.update_wlan(wlan_id, {"x_passphrase": pw})

    def create_wlan(self, name, password, band="both", vlan_id=None, hidden=False):
        payload = {"name": name, "x_passphrase": password, "security": "wpapsk",
                   "wpa_mode": "wpa2", "wpa_enc": "ccmp", "band": band,
                   "hide_ssid": hidden, "enabled": True}
        if vlan_id:
            payload.update(vlan_enabled=True, vlanid=vlan_id)
        return self.post(self.api("rest/wlanconf"), payload)

    def delete_wlan(self, wlan_id):
        return self.delete(self.api(f"rest/wlanconf/{wlan_id}"))

    # ── NETWORK WRITE ─────────────────────────────────────────────────────────
    def update_network(self, network_id, settings):
        return self.put(self.api(f"rest/networkconf/{network_id}"), settings)

    def create_network(self, name, subnet, vlan_id=None, dhcp=True, dhcp_start=None, dhcp_stop=None):
        payload = {"name": name, "ip_subnet": subnet, "dhcpd_enabled": dhcp, "purpose": "corporate"}
        if vlan_id:
            payload["vlanid"] = vlan_id
        if dhcp_start:
            payload["dhcpd_start"] = dhcp_start
        if dhcp_stop:
            payload["dhcpd_stop"] = dhcp_stop
        return self.post(self.api("rest/networkconf"), payload)

    def delete_network(self, network_id):
        return self.delete(self.api(f"rest/networkconf/{network_id}"))

    # ── FIREWALL / PORT FORWARD WRITE ─────────────────────────────────────────
    def create_firewall_rule(self, name, action, protocol, src_address="",
                             dst_address="", dst_port="", enabled=True):
        # Legacy (pre-zone-based) firewall. On consoles that use Zone-Based
        # Firewall these rules may not take effect - check the UniFi UI.
        return self.post(self.api("rest/firewallrule"), {
            "name": name, "action": action, "protocol": protocol,
            "src_address": src_address, "dst_address": dst_address,
            "dst_port": dst_port, "enabled": enabled, "ruleset": "LAN_IN"})

    def update_firewall_rule(self, rule_id, settings):
        return self.put(self.api(f"rest/firewallrule/{rule_id}"), settings)

    def delete_firewall_rule(self, rule_id):
        return self.delete(self.api(f"rest/firewallrule/{rule_id}"))

    def create_port_forward(self, name, dst_port, fwd_ip, fwd_port, protocol="tcp_udp"):
        return self.post(self.api("rest/portforward"), {
            "name": name, "dst_port": str(dst_port), "fwd": fwd_ip,
            "fwd_port": str(fwd_port), "proto": protocol, "enabled": True})

    def update_port_forward(self, pf_id, settings):
        return self.put(self.api(f"rest/portforward/{pf_id}"), settings)

    def delete_port_forward(self, pf_id):
        return self.delete(self.api(f"rest/portforward/{pf_id}"))

    # ── SITE ──────────────────────────────────────────────────────────────────
    def clear_alerts(self):
        return self.post(self.api("cmd/evtmgr"), {"cmd": "archive-all-alarms"})

    def speedtest(self):
        return self._devmgr("speedtest")

    def get_speedtest_status(self):
        return self._devmgr("speedtest-status")


def test_connection(profile, secret):
    """Work out the console type, check the login, list sites, capture the cert.

    Returns a dict for the UI. Never raises for expected failures.
    """
    if profile.get("console_type"):
        kinds = [profile["console_type"]]
    elif int(profile.get("port", 443)) == 8443:
        kinds = ["standalone", "unifi_os"]     # 8443 is almost always self-hosted
    else:
        kinds = ["unifi_os", "standalone"]
    pinned = profile.get("fingerprint")
    errors = []
    for kind in kinds:
        # Test without the pin so a changed cert is reported instead of blocking.
        c = UniFiClient(dict(profile, console_type=kind, fingerprint=None), secret)
        try:
            c.login()
            sites = c.list_sites()
            fp = c.last_fingerprint
            site_ids = [s["name"] for s in sites]
            wanted = profile.get("site") or "default"
            return {
                "ok": True,
                "console_type": kind,
                "console_label": CONSOLE_LABELS[kind],
                "sites": sites,
                "site_found": wanted in site_ids,
                "fingerprint": fp,
                "fingerprint_display": format_fingerprint(fp),
                "cert_changed": bool(pinned and fp and fp != pinned),
            }
        except UniFiError as e:
            errors.append(e)
            if e.kind in ("unreachable", "cert", "ratelimit", "mfa"):
                break      # same answer for either console type; don't hammer it
    # Prefer the most informative error (a login rejection beats "not found").
    errors.sort(key=lambda e: e.kind == "notfound")
    last_err = errors[0] if errors else None
    return {"ok": False, "kind": last_err.kind if last_err else "http",
            "error": str(last_err) if last_err else "Unknown error"}
