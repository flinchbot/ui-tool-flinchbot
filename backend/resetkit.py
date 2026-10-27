"""
Factory-reset kit: snapshot what a UniFi console is configured with, show how it
differs from a factory-fresh console, and write SSH commands that put any part of
it back after a reset.

Flow
  1. snapshot(client)            read every config collection (read-only)
  2. save(...)                   keep it in data/resetkit/ (secrets encrypted, key in
                                 the OS password store), because after the reset the
                                 console no longer has it
  3. variance(snap, baseline)    what is customised. With no baseline, objects you
                                 added are exact, and built-ins/settings show their
                                 current values. With a baseline (a snapshot of the
                                 freshly reset console) the comparison is exact.
  4. script(snap, ...)           a bash script to run in an SSH session on the
                                 console. One commented command per item; blocks
                                 that must run together say so.

Why commands on the console and not a restore from here: the user asked for SSH
commands they can pick from. The commands call the console's own API on
127.0.0.1 with curl, which is what the UniFi web UI does, so the controller
stays in charge of the config (editing its database directly would be undone
or corrupt it).

IDs change on a reset, so nothing in the script uses an old id. Every reference
(a Wi-Fi network's LAN, a policy's zone, a route's VPN client) is looked up by
name or built-in key when the command runs: $(ref rest/networkconf name 'IoT').
"""

import base64
import copy
import json
import os
import re
import secrets
import threading
from datetime import datetime

import config_store as cfg
from rollback import is_secret_key
from unifi_client import UniFiError

KIT_DIR = os.path.join(cfg.DATA_DIR, "resetkit")
_lock = threading.Lock()

# (section key, title, API path). Paths use the script's notation: rest/... is under
# /api/s/<site>/, v2:... under /v2/api/site/<site>/. Order = restore order.
COLLECTIONS = [
    ("networks",       "Networks, VLANs, WANs and VPN clients", "rest/networkconf"),
    ("zones",          "Firewall zones",                        "v2:firewall/zone"),
    ("wlan_groups",    "WLAN groups",                           "rest/wlangroup"),
    ("fw_groups",      "Firewall groups",                       "rest/firewallgroup"),
    ("policies",       "Firewall policies (zone-based)",        "v2:firewall-policies"),
    ("fw_rules",       "Firewall rules (legacy)",               "rest/firewallrule"),
    ("port_forwards",  "Port forwards",                         "rest/portforward"),
    ("routes",         "Static routes",                         "rest/routing"),
    ("traffic_routes", "Policy-based routes (traffic routes)",  "v2:trafficroutes"),
    ("traffic_rules",  "Traffic rules",                         "v2:trafficrules"),
    ("static_dns",     "Local DNS records",                     "v2:static-dns"),
    ("dyndns",         "Dynamic DNS",                           "rest/dynamicdns"),
    ("dhcp_options",   "Custom DHCP options",                   "rest/dhcpoption"),
    ("port_profiles",  "Switch port profiles",                  "rest/portconf"),
    ("user_groups",    "Client groups (bandwidth limits)",      "rest/usergroup"),
    ("member_groups",  "Network member groups",                 "v2:network-members-groups"),
    ("radius",         "RADIUS profiles",                       "rest/radiusprofile"),
    ("radius_users",   "RADIUS users",                          "rest/account"),
    ("ap_groups",      "AP groups",                             "v2:apgroups"),
    ("wlans",          "Wi-Fi networks",                        "rest/wlanconf"),
    ("schedules",      "Scheduled tasks",                       "rest/scheduletask"),
    ("clients",        "Clients: names, fixed IPs, notes",      "rest/user"),
    ("devices",        "UniFi devices: names, ports, radios",   "stat/device"),
    ("settings",       "Site settings",                         "rest/setting"),
]
TITLES = {k: t for k, t, _ in COLLECTIONS}
PATHS = {k: p for k, _, p in COLLECTIONS}
NOUN = {"networks": "network", "zones": "firewall zone", "wlan_groups": "WLAN group", "policies": "firewall policy",
        "fw_groups": "firewall group", "fw_rules": "firewall rule", "port_forwards": "port forward",
        "routes": "static route", "traffic_routes": "policy-based route", "traffic_rules": "traffic rule",
        "static_dns": "DNS record", "dyndns": "dynamic DNS entry", "dhcp_options": "DHCP option",
        "port_profiles": "port profile", "user_groups": "client group", "member_groups": "member group",
        "radius": "RADIUS profile", "radius_users": "RADIUS user", "ap_groups": "AP group", "wlans": "Wi-Fi network",
        "schedules": "scheduled task", "clients": "client", "devices": "device", "settings": "settings section"}
# Kept only so references to them resolve; listed only when you added one.
REF_ONLY = {"wlan_groups"}

# Read-only / server-assigned fields never sent back.
_DROP = {"_id", "site_id", "external_id", "hits", "last_hit", "index", "predefined", "origin_id",
         "origin_type", "attr_hidden", "attr_no_edit", "wireguard_id",
         "firewall_zone_id",      # zone membership is set on the zone (zones section)
         # Keys the console generates for itself. Writing old ones onto a reset console
         # would clash with what it just made (device adoption, mesh, API token...).
         "x_iapp_key", "x_mgmt_key", "x_api_token", "x_mesh_essid", "x_mesh_psk", "x_element_essid",
         "x_element_psk", "x_pregenerated_dh_key", "utm_token", "x_ssh_sha512passwd"}
# magic_site_to_site_vpn's private key is generated per console too
_DROP_IN = {"magic_site_to_site_vpn": {"x_private_key"}, "peer_to_peer": {"psk"}}

# Friendlier names for the secret fields the script asks for.
SECRET_LABELS = {"x_passphrase": "Wi-Fi password", "wireguard_client_configuration_file": "WireGuard config file",
                 "x_password": "password", "x_secret": "shared secret", "x_ssh_password": "SSH password",
                 "private_preshared_keys": "private pre-shared keys", "x_uplink_password": "WAN login password",
                 "x_wireguard_private_key": "WireGuard private key", "x_shared_secret": "shared secret",
                 "x_ssh_username": "device SSH username"}

# Client (rest/user) fields that are the user's own customisation.
CLIENT_FIELDS = ["name", "note", "noted", "use_fixedip", "fixed_ip", "network_id", "usergroup_id",
                 "local_dns_record", "local_dns_record_enabled", "fixed_ap_enabled", "fixed_ap_mac",
                 "virtual_network_override_enabled", "virtual_network_override_id",
                 "network_members_group_ids"]
# Device (stat/device) fields that are configuration rather than live state.
DEVICE_FIELDS = ["name", "port_overrides", "radio_table", "led_override", "led_override_color",
                 "led_override_color_brightness", "outlet_overrides", "config_network",
                 "snmp_contact", "snmp_location", "stp_priority",
                 "jumboframe_enabled", "flowctrl_enabled", "dot1x_portctrl_enabled"]
# Facts about a factory-fresh UniFi console that hold across current versions.
KNOWN_DEFAULTS = {("networks", "attr_hidden_id:LAN"): {"ip_subnet": "192.168.1.1/24", "dhcpd_start": "192.168.1.6",
                                                       "dhcpd_stop": "192.168.1.254", "name": "Default"}}

SECRET_MARK = "<secret>"
SECRET_JSON = "<secret-json>"          # a secret that isn't a string (e.g. a list of keys)
_MASKS = (SECRET_MARK, SECRET_JSON)


class KitError(ValueError):        # shown to the user as a 400 with this message
    pass


# ── 1. snapshot ───────────────────────────────────────────────────────────────
def _data(resp):
    if isinstance(resp, dict) and "data" in resp:
        resp = resp["data"]
    return [o for o in resp if isinstance(o, dict)] if isinstance(resp, list) else []


def _get(client, path):
    if path.startswith("v2:"):
        return _data(client.get(client.v2(path[3:])))
    return _data(client.get(client.api(path)))


def _client_custom(u):
    return bool(u.get("name") or u.get("noted") or u.get("use_fixedip") or u.get("local_dns_record_enabled")
                or u.get("fixed_ap_enabled") or u.get("virtual_network_override_enabled") or u.get("blocked")
                or u.get("network_members_group_ids"))


def snapshot(client, profile):
    """Read every config collection. Read-only. -> snapshot dict (holds secrets)."""
    sections, unavailable = {}, {}
    for key, _title, path in COLLECTIONS:
        try:
            items = _get(client, path)
        except UniFiError as e:
            if e.kind in ("unreachable", "cert", "auth", "noconn"):
                raise
            unavailable[key] = str(e)
            continue
        if key == "clients":
            items = [dict({k: u[k] for k in CLIENT_FIELDS if k in u}, mac=u.get("mac"), hostname=u.get("hostname"),
                          oui=u.get("oui"), blocked=bool(u.get("blocked")), _id=u.get("_id"))
                     for u in items if u.get("mac") and _client_custom(u)]
        elif key == "devices":
            items = [dict({k: d[k] for k in DEVICE_FIELDS if k in d}, mac=d.get("mac"), model=d.get("model"),
                          type=d.get("type"), _id=d.get("_id"), version=d.get("version"))
                     for d in items if d.get("mac")]
        sections[key] = items
    version = None
    try:
        info = client.get_system_info()
        version = (info[0] if isinstance(info, list) and info else {}).get("version")
    except UniFiError:
        pass
    return {"format": 1, "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "connection_id": profile["id"], "connection": profile["name"], "host": profile["host"],
            "site": profile.get("site", "default"), "console_type": profile.get("console_type") or "unifi_os",
            "port": profile.get("port", 443), "network_version": version,
            "sections": sections, "unavailable": unavailable}


# ── 2. storage (secrets encrypted; key in the OS password store) ──────────────
def split(obj):
    """-> (copy with secrets replaced by a marker, {json path: value})."""
    found = {}

    def walk(o, path):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if is_secret_key(k) and v not in (None, "", [], {}) and not isinstance(v, (bool, int, float)):
                    found[json.dumps(path + [k])] = v
                    out[k] = SECRET_MARK if isinstance(v, str) else SECRET_JSON
                else:
                    out[k] = walk(v, path + [k])
            return out
        if isinstance(o, list):
            return [walk(v, path + [i]) for i, v in enumerate(o)]
        return o
    return walk(obj, []), found


def join(obj, found):
    obj = copy.deepcopy(obj)
    for p, v in (found or {}).items():
        path, cur = json.loads(p), obj
        try:
            for step in path[:-1]:
                cur = cur[step]
            cur[path[-1]] = v
        except (KeyError, IndexError, TypeError):
            pass
    return obj


def _fernet(key):
    from cryptography.fernet import Fernet      # installed with paramiko
    return Fernet(key)


def save(snap):
    """Write the snapshot. -> its id. Secrets are encrypted with a key kept in the OS
    password store, so the JSON file on disk never holds a password or private key."""
    os.makedirs(KIT_DIR, exist_ok=True)
    sid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    public, found = split(snap)
    public["id"] = sid
    if found:
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        cfg.set_secret(f"kit-{sid}", key)
        public["secrets_encrypted"] = _fernet(key.encode()).encrypt(json.dumps(found).encode()).decode()
        public["secret_count"] = len(found)
    with _lock:
        tmp = os.path.join(KIT_DIR, f"{sid}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(public, f, indent=1, ensure_ascii=False)
        os.replace(tmp, os.path.join(KIT_DIR, f"{sid}.json"))
    return sid


def _path(sid):
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", str(sid or "")):
        raise KitError("Unknown snapshot.")
    return os.path.join(KIT_DIR, f"{sid}.json")


def load(sid, with_secrets=False):
    try:
        with open(_path(sid), encoding="utf-8") as f:
            public = json.load(f)
    except FileNotFoundError:
        raise KitError("That snapshot no longer exists.")
    if not with_secrets or not public.get("secrets_encrypted"):
        return public
    key = cfg.get_secret(f"kit-{sid}")
    if not key:
        raise KitError("The key for this snapshot's passwords is missing from the password store, so "
                       "passwords and VPN keys can't be shown or written into the script. Everything else still works.")
    try:
        found = json.loads(_fernet(key.encode()).decrypt(public["secrets_encrypted"].encode()))
    except Exception:
        raise KitError("This snapshot's passwords couldn't be decrypted.")
    return join(public, found)


def secrets_of(sid):
    """[{section, object, field, value}] for 'Show passwords and keys'."""
    full, public = load(sid, with_secrets=True), load(sid)
    out = []
    for key, items in public.get("sections", {}).items():
        for i, o in enumerate(items):
            for k, v in o.items():
                if v in _MASKS:
                    val = full["sections"][key][i][k]
                    out.append({"section": TITLES.get(key, key), "object": _label(key, o),
                                "field": SECRET_LABELS.get(k, k),
                                "value": val if isinstance(val, str) else json.dumps(val)})
    return out


def list_snapshots(connection_id=None):
    out = []
    if not os.path.isdir(KIT_DIR):
        return out
    for name in sorted(os.listdir(KIT_DIR), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(KIT_DIR, name), encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        if connection_id and s.get("connection_id") != connection_id:
            continue
        out.append({"id": s.get("id"), "taken_at": s.get("taken_at"), "connection": s.get("connection"),
                    "connection_id": s.get("connection_id"), "host": s.get("host"),
                    "network_version": s.get("network_version"),
                    "counts": {k: len(v) for k, v in s.get("sections", {}).items()},
                    "secret_count": s.get("secret_count", 0)})
    return out


def delete(sid):
    p = _path(sid)
    if os.path.exists(p):
        os.remove(p)
    cfg.delete_secret(f"kit-{sid}", kind="")


# ── identity: how an object is found again after a reset ──────────────────────
def identity(section, o):
    """-> (field, value) that names this object on a reset console too."""
    if section in ("clients", "devices"):
        return ("mac", o.get("mac"))
    if section == "settings":
        return ("key", o.get("key"))
    if o.get("attr_hidden_id"):
        return ("attr_hidden_id", o["attr_hidden_id"])
    if section == "zones" and o.get("zone_key"):
        return ("zone_key", o["zone_key"])
    if section == "networks" and o.get("purpose") == "wan" and o.get("wan_networkgroup"):
        return ("wan_networkgroup", o["wan_networkgroup"])
    if section == "traffic_routes" and not o.get("name"):
        return ("description", o.get("description"))
    if section == "static_dns" and not o.get("name"):
        return ("key", o.get("key"))
    if section == "schedules":
        return ("name", o.get("name") or o.get("action"))
    return ("name", o.get("name"))


def builtin(section, o):
    if section == "settings":
        return True
    if section == "zones":
        return bool(o.get("default_zone") or o.get("zone_key"))
    if section == "policies":
        return bool(o.get("predefined"))
    return bool(o.get("attr_no_delete") or o.get("attr_hidden_id"))


def _label(section, o):
    if section == "clients":
        return f"{o.get('name') or o.get('hostname') or o.get('mac')} ({o.get('mac')})"
    if section == "devices":
        return f"{o.get('name') or o.get('model') or o.get('mac')} ({o.get('mac')})"
    if section == "settings":
        return o.get("key") or "?"
    if section == "traffic_routes":
        return o.get("description") or o.get("name") or "route"
    if section == "static_dns":
        return o.get("key") or o.get("name") or "record"
    return o.get("name") or o.get("description") or o.get("key") or o.get("_id") or "?"


# ── 3. variance ───────────────────────────────────────────────────────────────
def _cmp(v):
    return json.dumps(v, sort_keys=True)


def fields_of(o):
    return {k: v for k, v in o.items() if k not in _DROP and not k.startswith("attr_")}


def _device_custom(fields):
    """Only the device settings that differ from the out-of-the-box state."""
    out = {}
    for k, v in fields.items():
        if k in ("name", "port_overrides", "outlet_overrides", "snmp_contact", "snmp_location") and v:
            out[k] = v
        elif k == "radio_table" and any(isinstance(r, dict) and (str(r.get("channel", "auto")) != "auto"
                                                                 or r.get("tx_power_mode", "auto") != "auto")
                                        for r in v or []):
            out[k] = v
        elif k == "config_network" and isinstance(v, dict) and v.get("type") == "static":
            out[k] = v
        elif k == "led_override" and v not in (None, "", "default"):
            out[k] = v
        elif k in ("led_override_color", "led_override_color_brightness") and fields.get("led_override") not in (None, "", "default"):
            out[k] = v
        elif k == "stp_priority" and str(v) not in ("32768", "0", ""):
            out[k] = v
        elif k in ("jumboframe_enabled", "flowctrl_enabled", "dot1x_portctrl_enabled") and v:
            out[k] = v
    return out


def _item_fields(sec, o):
    f = fields_of(o)
    if sec == "settings":
        f = {k: v for k, v in f.items() if k not in _DROP_IN.get(o.get("key"), ())}
    if sec == "devices":
        return _device_custom({k: v for k, v in f.items() if k in DEVICE_FIELDS})
    if sec == "clients":
        return _client_fields(f)
    return f


def _client_fields(f):
    """A client's own settings, without empty values and settings that are switched off."""
    out = {k: v for k, v in f.items() if (k in CLIENT_FIELDS or k == "blocked") and v not in (None, "", False, [])}
    for flag, deps in (("use_fixedip", ("fixed_ip", "network_id")), ("fixed_ap_enabled", ("fixed_ap_mac",)),
                       ("local_dns_record_enabled", ("local_dns_record",)),
                       ("virtual_network_override_enabled", ("virtual_network_override_id",))):
        if not out.get(flag):
            for d in deps:
                out.pop(d, None)
    if not out.get("noted"):
        out.pop("note", None)
    return out


def variance(snap, baseline=None):
    """-> {"items": [...], "summary": {...}, "exact": bool, "unavailable": {...}}.

    Each item: id, section, title, label, kind ("added" | "changed" | "current"),
    fields {field: value} (secrets masked), changed_fields, notes.
    """
    base_idx = {}
    if baseline:
        for sec, objs in baseline.get("sections", {}).items():
            for o in objs:
                base_idx[(sec,) + identity(sec, o)] = o
    items = []
    for sec, _title, _path in COLLECTIONS:
        for o in snap.get("sections", {}).get(sec, []):
            if sec == "policies" and o.get("predefined"):
                continue
            if sec in REF_ONLY and builtin(sec, o):
                continue
            ident = identity(sec, o)
            if ident[1] in (None, ""):
                continue
            fields = _item_fields(sec, o)
            if sec == "devices" and not fields:
                continue
            item = {"id": f"{sec}:{ident[0]}:{ident[1]}", "section": sec, "title": TITLES[sec],
                    "label": _label(sec, o), "notes": [], "fields": fields,
                    "_order": o.get("index") if isinstance(o.get("index"), int) else 0}
            per_object = sec in ("devices", "clients")
            if baseline is not None:
                b = base_idx.get((sec,) + ident)
                if b is None:
                    item["kind"] = "changed" if per_object or sec == "settings" else "added"
                    if per_object:
                        item["changed_fields"] = sorted(fields)
                else:
                    bf = _item_fields(sec, b)
                    diff = sorted(k for k, v in fields.items() if v not in _MASKS and _cmp(v) != _cmp(bf.get(k)))
                    if not diff:
                        continue
                    item["kind"] = "changed"
                    item["changed_fields"] = diff
            elif per_object:
                item["kind"] = "changed"
                item["changed_fields"] = sorted(fields)
            elif builtin(sec, o):
                known = KNOWN_DEFAULTS.get((sec, f"{ident[0]}:{ident[1]}"))
                item["kind"] = "current"
                if known:
                    diff = sorted(k for k, v in known.items() if fields.get(k) != v)
                    if diff:
                        item["kind"] = "changed"
                        item["changed_fields"] = diff
                        item["notes"].append("Factory default: " + ", ".join(f"{k} {known[k]}" for k in diff))
            else:
                item["kind"] = "added"
            items.append(item)
    order = {k: i for i, (k, _t, _p) in enumerate(COLLECTIONS)}
    items.sort(key=lambda it: (order[it["section"]], it["_order"]))
    for it in items:
        it.pop("_order", None)
        it["summary"] = _summ(it["section"], it["fields"])
    summary = {k: sum(1 for i in items if i["kind"] == k) for k in ("added", "changed", "current")}
    return {"items": items, "summary": summary, "exact": baseline is not None,
            "unavailable": snap.get("unavailable", {})}


# ── 4. the SSH script ─────────────────────────────────────────────────────────
def sq(s):
    """Shell single-quote."""
    return "'" + str(s).replace("'", "'\\''") + "'"


# Sections other objects point at, so their old ids can be turned into lookups.
_REF_SECTIONS = ("networks", "zones", "wlan_groups", "fw_groups", "port_profiles", "user_groups", "member_groups",
                 "radius", "ap_groups", "wlans")


def _ref_index(snap):
    idx = {}
    for sec in _REF_SECTIONS:
        for o in snap.get("sections", {}).get(sec, []):
            if o.get("_id"):
                f, v = identity(sec, o)
                if v not in (None, ""):
                    idx[o["_id"]] = (PATHS[sec], f, v, f"{NOUN[sec]} “{_label(sec, o)}”")
    return idx


class _Emitter:
    """Turns an object into one shell word: single-quoted JSON with $(ref ...) and
    $(secret ...) spliced in where an old id or a password goes."""

    def __init__(self, idx):
        self.idx = idx
        self.unknown = set()
        self.prompts = []

    def body(self, obj, what):
        parts = []                          # ("lit", json text) | ("expr", shell expression)

        def splice(expr, quoted):
            # Refs and string secrets sit where a JSON string goes, so they get "..." around them.
            if quoted:
                lit('"')
            parts.append(("expr", expr))
            if quoted:
                lit('"')

        def lit(t):
            if parts and parts[-1][0] == "lit":
                parts[-1] = ("lit", parts[-1][1] + t)
            else:
                parts.append(("lit", t))

        def walk(o, key=None):
            if isinstance(o, dict):
                lit("{")
                for n, (k, v) in enumerate(o.items()):
                    lit(("," if n else "") + json.dumps(k) + ":")
                    walk(v, k)
                lit("}")
            elif isinstance(o, list):
                lit("[")
                for n, v in enumerate(o):
                    if n:
                        lit(",")
                    walk(v, key)
                lit("]")
            elif isinstance(o, str) and o in _MASKS:
                kind = "json" if o == SECRET_JSON else "multi" if "configuration" in str(key) else "line"
                label = f"{what}: {SECRET_LABELS.get(key, key)}".replace("“", '"').replace("”", '"')
                fn = {"line": "secret", "multi": "secret_multi", "json": "secret_json"}[kind]
                splice(f'"$({fn} {sq(label)})"', kind != "json")
                self.prompts.append(label)
            elif isinstance(o, str) and o in self.idx:
                path, f, v, _ = self.idx[o]
                splice(f'"$(ref {path} {f} {sq(v)})"', True)
            elif isinstance(o, str) and re.fullmatch(r"[0-9a-f]{24}", o):
                self.unknown.add(o)
                lit(json.dumps(o))
            else:
                lit(json.dumps(o, ensure_ascii=False))
        walk(obj)
        return "".join(sq(w) if k == "lit" else w for k, w in parts)


def _embed(obj, full):
    """Replace masked secrets in obj with the real values from full (same shape)."""
    if isinstance(obj, dict):
        full = full if isinstance(full, dict) else {}
        return {k: (full.get(k) if v in _MASKS else _embed(v, full.get(k))) for k, v in obj.items()}
    if isinstance(obj, list):
        full = full if isinstance(full, list) else []
        return [_embed(v, full[i] if i < len(full) else None) for i, v in enumerate(obj)]
    return obj


SETUP = r'''
# ┌───────────────────────────────────────────────────────────────────────────┐
# │ SETUP - RUN TOGETHER, first, in the same SSH session as everything you    │
# │ run after it. Paste the whole block at once.                              │
# │ It asks for a LOCAL UniFi admin's username and password, signs in to the  │
# │ console's own API on 127.0.0.1 and defines the helpers used below:        │
# │   api METHOD PATH [JSON]   call the API; prints OK or FAILED with why     │
# │   ref PATH FIELD VALUE     the current id of the object with FIELD=VALUE  │
# │   secret / secret_multi    ask for a password, or paste a key or config   │
# │ This block changes nothing.                                               │
# └───────────────────────────────────────────────────────────────────────────┘
UNIFI=__BASE__; SITE=__SITE__; PREFIX=__PREFIX__; LOGIN=__LOGIN__
FB=$(mktemp -d); JAR="$FB/cookies"; HDR="$FB/headers"
jesc() { tr -d '\r' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' | awk 'NR>1{printf "%s","\\n"} {printf "%s",$0}'; }
printf 'Local UniFi admin username: '; read -r FB_USER
printf 'Password: '; stty -echo 2>/dev/null; read -r FB_PASS; stty echo 2>/dev/null; echo
curl -sk -o "$FB/login" -D "$HDR" -c "$JAR" -H 'Content-Type: application/json' \
  --data-binary "{\"username\":\"$(printf %s "$FB_USER" | jesc)\",\"password\":\"$(printf %s "$FB_PASS" | jesc)\",\"rememberMe\":false}" \
  "$UNIFI$LOGIN"; unset FB_PASS
CSRF=$(grep -i '^x-csrf-token:' "$HDR" | head -n1 | cut -d' ' -f2 | tr -d '\r')
if grep -Eq 'TOKEN|unifises' "$JAR"; then echo "Signed in."; else echo "Sign-in FAILED: $(head -c 300 "$FB/login")"; fi
api() {
  m=$1; p=$2; data=$3
  case $p in
    v2:*) url="$UNIFI$PREFIX/v2/api/site/$SITE/${p#v2:}" ;;
    *)    url="$UNIFI$PREFIX/api/s/$SITE/$p" ;;
  esac
  if [ -n "$data" ]; then
    code=$(curl -sk -o "$FB/out" -w '%{http_code}' -X "$m" -b "$JAR" -c "$JAR" -H "X-CSRF-Token: $CSRF" \
           -H 'Content-Type: application/json' --data-binary "$data" "$url")
  else
    code=$(curl -sk -o "$FB/out" -w '%{http_code}' -X "$m" -b "$JAR" -c "$JAR" -H "X-CSRF-Token: $CSRF" "$url")
  fi
  case $code in
    2??) if [ "$m" = GET ]; then cat "$FB/out"; else echo "  OK ($m $p)" >&2; fi; return 0 ;;
    *)   echo "  FAILED: HTTP $code from $m $p: $(head -c 400 "$FB/out")" >&2; return 1 ;;
  esac
}
jfind() {  # jfind FIELD VALUE < JSON  ->  _id of the first object whose FIELD equals VALUE
  awk -v want_k="$1" -v want_v="$2" '
    function val(v) { if (key == "_id") cur = v; if (key == want_k && v == want_v) hit = 1 }
    function flushlit() { if (lit != "") { val(lit); lit = "" } }
    BEGIN { RS = "\001" }
    { s = s $0 }
    END {
      n = length(s); depth = 0; D = 0; instr = 0
      for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (instr) {
          if (c == "\\") { i++; e = substr(s, i, 1); buf = buf (e == "n" ? "\n" : e == "t" ? "\t" : e); continue }
          if (c == "\"") { instr = 0; if (depth == D) { if (expect_key) { key = buf; expect_key = 0 } else val(buf) }; continue }
          buf = buf c; continue
        }
        if (c == "\"") { instr = 1; buf = ""; continue }
        if (D == 0 && (c == "[" || c == "{")) D = (c == "[") ? 2 : 3
        if (c == "{" || c == "[") { depth++; if (depth == D && c == "{") { cur = ""; hit = 0; expect_key = 1; lit = "" }; continue }
        if (c == "}" || c == "]") { if (depth == D) { flushlit(); if (c == "}" && hit && cur != "") { print cur; exit } }; depth--; continue }
        if (depth == D) {
          if (c == ",") { flushlit(); expect_key = 1; continue }
          if (c == ":") { lit = ""; continue }
          if (c == " " || c == "\t" || c == "\r" || c == "\n") continue
          lit = lit c
        }
      }
    }'
}
ref() {  # ref PATH FIELD VALUE  ->  the object's id now (looked up, because ids change on a reset)
  id=$(api GET "$1" | jfind "$2" "$3")
  if [ -z "$id" ]; then echo "  NOT FOUND: nothing in $1 has $2 = $3. Restore that first (see its section)." >&2; id=MISSING; fi
  printf %s "$id"
}
secret() {  # secret LABEL  ->  asks for one line (not shown), prints it JSON-escaped
  printf '%s: ' "$1" >&2; stty -echo 2>/dev/null; read -r v; stty echo 2>/dev/null; echo >&2
  printf %s "$v" | jesc
}
secret_multi() {  # secret_multi LABEL  ->  paste several lines, then a line with just END
  echo "$1 - paste it, then type END on its own line:" >&2
  v=""; while IFS= read -r line; do line=$(printf %s "$line" | tr -d '\r'); [ "$line" = END ] && break; v="$v$line
"; done
  printf %s "$v" | jesc; printf '%s' '\n'     # jesc drops the last line break; put it back
}
secret_json() {  # secret_json LABEL  ->  paste a JSON value on one line, used as-is
  printf '%s (JSON, one line): ' "$1" >&2; read -r v; printf %s "$v"
}
'''

_SECTION_NOTES = {
    "networks": ["New networks are created; built-in ones (Default LAN, WANs) are updated in place."],
    "zones": ["Run after the networks: a custom zone is created with its networks in it."],
    "policies": ["In their original order. Each needs its zones (and any networks it names)."],
    "wlans": ["Each Wi-Fi network needs its LAN/VLAN and AP group to exist first."],
    "clients": ["A client that hasn't reconnected yet is created by MAC; one that has is updated."],
    "devices": ["Adopt the devices first. Each command updates one device, found by its MAC."],
    "settings": ["Each command writes one settings section. Run only the ones you want back."],
}


def _section_header(title, lines=()):
    bar = "#" + "═" * 78
    return ["", bar, f"# {title}"] + [f"# {l}" for l in lines] + [bar]


def _wrap(text, width=78):
    lines, cur = [], ""
    for w in str(text).split():
        if cur and len(cur) + len(w) + 1 > width - 2:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return [f"# {l}" for l in lines]


def _summ(sec, f):
    """A short description of an object's main settings, for comments and the report."""
    g = f.get
    if sec == "networks":
        if g("purpose") == "vpn-client":
            return (f"VPN client ({g('vpn_type') or 'vpn'}), tunnel address {g('ip_subnet')}, "
                    f"{'enabled' if g('enabled') else 'disabled'}")
        if g("purpose") == "wan":
            return f"WAN, {g('wan_type') or '?'}" + (f", VLAN {g('wan_vlan')}" if g("wan_vlan_enabled") else "")
        bits = [g("purpose") or "network", g("ip_subnet") or ""]
        if g("vlan_enabled") or g("vlan"):
            bits.append(f"VLAN {g('vlan')}")
        if g("dhcpd_enabled"):
            bits.append(f"DHCP {g('dhcpd_start')} - {g('dhcpd_stop')}")
        if g("domain_name"):
            bits.append(f"domain {g('domain_name')}")
        return ", ".join(b for b in bits if b)
    if sec == "wlans":
        bands = "/".join(g("wlan_bands") or []) or "all bands"
        mode = (g("wpa_mode") or g("security") or "").upper() + ("/WPA3" if g("wpa3_transition") else "")
        return (f"{mode}, {bands}"
                f"{', hidden' if g('hide_ssid') else ''}{', guest' if g('is_guest') else ''}")
    if sec == "policies":
        s, d = g("source") or {}, g("destination") or {}
        return (f"{g('action')} {g('protocol')} from {s.get('matching_target', '?')} to {d.get('matching_target', '?')}"
                f"{' port ' + str(d.get('port')) if d.get('port') else ''}{' (disabled)' if g('enabled') is False else ''}")
    if sec == "zones":
        return f"{len(g('network_ids') or [])} network(s)"
    if sec == "port_forwards":
        return f"WAN port {g('dst_port')} to {g('fwd')}:{g('fwd_port')} ({g('proto')})"
    if sec == "traffic_routes":
        return f"{g('matching_target')} traffic through another WAN/VPN" + (", kill switch" if g("kill_switch_enabled") else "")
    if sec == "clients":
        bits = []
        if g("name"):
            bits.append(f"name “{g('name')}”")
        if g("use_fixedip"):
            bits.append(f"fixed IP {g('fixed_ip')}")
        if g("noted"):
            bits.append("note")
        if g("local_dns_record_enabled"):
            bits.append(f"DNS name {g('local_dns_record')}")
        if g("blocked"):
            bits.append("BLOCKED")
        return ", ".join(bits)
    if sec == "devices":
        bits = []
        if g("name"):
            bits.append(f"name “{g('name')}”")
        if g("port_overrides"):
            bits.append(f"{len(g('port_overrides'))} port setting(s)")
        if g("radio_table"):
            bits.append("radio channels/power")
        if g("config_network"):
            bits.append(f"static IP {(g('config_network') or {}).get('ip')}")
        return ", ".join(bits)
    if sec == "schedules":
        return f"{g('action')} at cron '{g('cron_expr')}'"
    if sec == "settings":
        return f"{len([k for k in f if k != 'key'])} values"
    return ""


def script(snap, full=None, selected=None, embed_secrets=False, baseline=None):
    """The SSH script as text, plus stats. `snap` has secrets masked; `full` (the
    decrypted snapshot) is used only when embed_secrets is True."""
    v = variance(snap, baseline)
    items = [it for it in v["items"] if selected is None or it["id"] in selected]
    by_id = {}
    for sec, objs in snap.get("sections", {}).items():
        for i, o in enumerate(objs):
            f, val = identity(sec, o)
            by_id[f"{sec}:{f}:{val}"] = (sec, i, o)
    em = _Emitter(_ref_index(snap))
    unifi_os = (snap.get("console_type") or "unifi_os") == "unifi_os"
    port = int(snap.get("port") or 443)
    # On the console itself: UniFi OS answers on 443; a self-hosted Network app on its own port (8443 by default).
    base = "https://127.0.0.1" if unifi_os else f"https://127.0.0.1:{port if port != 443 else 8443}"
    setup = (SETUP.replace("__BASE__", sq(base)).replace("__SITE__", sq(snap.get("site") or "default"))
             .replace("__PREFIX__", sq("/proxy/network" if unifi_os else ""))
             .replace("__LOGIN__", sq("/api/auth/login" if unifi_os else "/api/login")))
    out = [
        "#!/bin/bash",
        f"# UniFi restore commands for “{snap.get('connection')}” ({snap.get('host')}, site {snap.get('site')})",
        f"# Snapshot taken {snap.get('taken_at')}"
        + (f" from UniFi Network {snap.get('network_version')}" if snap.get("network_version") else "")
        + " by UniFi × flinchbot.",
        "# " + ("Only what differs from the reset console." if v["exact"] else
                "Everything you customised; built-in sections are written back with all their values."),
        "#",
        "# HOW TO USE",
        "#  1. After the factory reset, finish UniFi's setup. Create a LOCAL admin",
        "#     (Settings → Admins & Users → Restrict to local access) and turn on SSH",
        "#     (UniFi OS: Settings → Control Plane → Console → SSH). Then:",
        "#        ssh root@<console address>",
        "#  2. Paste the SETUP block. It must be run together, and first.",
        "#  3. Run any commands below that you want back, top to bottom. Each one is a",
        "#     single line that stands alone, except blocks marked RUN TOGETHER.",
        "#     References (a Wi-Fi network's LAN, a policy's zone) are looked up by",
        "#     name when the command runs, so restore what it needs first; each",
        "#     comment says what that is.",
        "#  4. Every command prints OK, or FAILED with the console's answer.",
        "#     Paste a command that asks for a password ON ITS OWN: anything pasted",
        "#     after it would be read as the answer.",
        "#  5. Adopt your UniFi devices again BEFORE the devices section.",
        "#  6. The RUN LAST section changes the console's own LAN address, so your SSH",
        "#     session drops there. Reconnect at the new address.",
        "#",
    ]
    if embed_secrets:
        out += ["# !! This file CONTAINS PASSWORDS AND VPN PRIVATE KEYS. Keep it private and",
                "# !! delete it when you're done."]
    else:
        out += ["# Passwords, Wi-Fi keys and VPN configs are NOT in this file. Commands that",
                "# need one ask for it; the app shows them under Reset kit → Show passwords."]
    out += setup.splitlines()

    last, current = [], None
    for it in items:
        sec, i, obj = by_id[it["id"]]
        f = _item_fields(sec, obj)
        if sec == "clients":
            f = {k: x for k, x in f.items() if k in CLIENT_FIELDS}
        elif v["exact"] and builtin(sec, obj) and it.get("changed_fields"):
            f = {k: f[k] for k in it["changed_fields"] if k in f}
        if embed_secrets and full is not None:
            f = _embed(f, _item_fields(sec, full["sections"][sec][i]))
        comment, cmds = _command(sec, obj, f, it, em)
        moves_console = (sec == "networks" and obj.get("attr_hidden_id") == "LAN"
                         and any(k in f for k in ("ip_subnet", "dhcpd_start", "dhcpd_stop")))
        if moves_console:
            last += [""] + comment + cmds
            continue
        if sec != current:
            out += _section_header(TITLES[sec], _SECTION_NOTES.get(sec, ()))
            current = sec
        out += [""] + comment + cmds
    if last:
        out += _section_header("RUN LAST: the console's own LAN",
                               ["This moves the console to a new address and your SSH session drops.",
                                "Reconnect at the new address. Computers on the LAN need a new DHCP",
                                "lease (unplug and replug, or turn Wi-Fi off and on)."]) + last
    if em.unknown:
        out += ["", "# NOTE: these ids in the commands above point at something the snapshot doesn't",
                "# have, so they can't be looked up by name. Check those settings in the UniFi UI:",
                *[f"#   {u}" for u in sorted(em.unknown)]]
    out += ["", "# Done. Sign out of the API session and remove the temporary files (optional):",
            "curl -sk -o /dev/null -X POST -b \"$JAR\" -H \"X-CSRF-Token: $CSRF\" \"$UNIFI/api/auth/logout\"; rm -rf \"$FB\"", ""]
    return "\n".join(out), {"items": len(items), "prompts": len(em.prompts), "unknown_ids": sorted(em.unknown)}


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield k, v
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _deps(o, idx):
    if isinstance(o, dict):
        for v in o.values():
            yield from _deps(v, idx)
    elif isinstance(o, list):
        for v in o:
            yield from _deps(v, idx)
    elif isinstance(o, str) and o in idx:
        yield idx[o][3]


def _command(sec, obj, f, it, em):
    """-> (comment lines, command lines) for one item."""
    label, path = it["label"], PATHS[sec]
    what = f"{NOUN[sec]} “{label}”"
    verb = {"added": "Create", "changed": "Put back", "current": "Restore"}[it["kind"]]
    summary = _summ(sec, f)
    head = f"{verb} {what}" + (f": {summary}" if summary else "") + "."
    if it.get("changed_fields") and it["kind"] == "changed" and sec != "clients":
        cf = it["changed_fields"]
        head += f" Changed: {', '.join(cf[:12])}{'…' if len(cf) > 12 else ''}."
    comment = _wrap(head)
    for n in it.get("notes") or []:
        comment += _wrap(n)
    deps = sorted(set(_deps(f, em.idx)))
    if deps:
        comment += _wrap("Needs first: " + "; ".join(deps) + ".")
    asks = sorted({SECRET_LABELS.get(k, k) for k, x in _walk(f) if x in _MASKS})
    if asks:
        comment += _wrap("Asks for: " + ", ".join(asks) + " (Reset kit → Show passwords).")

    if sec == "clients":
        mac = obj.get("mac")
        cmds = ["# RUN TOGETHER (2 lines): find the client by MAC, then update it, or create it if it hasn't",
                "# reconnected yet.",
                f"FB_ID=$(api GET stat/user/{mac} 2>/dev/null | jfind mac {sq(mac)})",
                f"if [ -n \"$FB_ID\" ]; then api PUT \"rest/user/$FB_ID\" {em.body(f, what)}; "
                f"else api POST rest/user {em.body(dict(f, mac=mac), what)}; fi"]
        if obj.get("blocked"):
            cmds += ["# It was blocked: block it again.",
                     f"api POST cmd/stamgr {sq(json.dumps({'cmd': 'block-sta', 'mac': mac}))}"]
        return comment, cmds
    if sec == "devices":
        mac = obj.get("mac")
        return comment, [f"api PUT \"rest/device/$(api GET stat/device/{mac} | jfind mac {sq(mac)})\" {em.body(f, what)}"]
    if sec == "settings":
        key = obj.get("key")
        return comment, [f"api PUT \"rest/setting/{key}/$(ref rest/setting key {sq(key)})\" {em.body(dict(f, key=key), what)}"]
    if it["kind"] == "added":
        return comment, [f"api POST {path} {em.body(f, what)}"]
    idf, idv = identity(sec, obj)              # a built-in object: update it in place
    return comment, [f"api PUT \"{path}/$(ref {path} {idf} {sq(idv)})\" {em.body(f, what)}"]


# ── report ────────────────────────────────────────────────────────────────────
def report_markdown(snap, v):
    s = v["summary"]
    out = ["# UniFi configuration: what's customised", "",
           f"- **Network:** {snap.get('connection')} ({snap.get('host')}, site {snap.get('site')})",
           f"- **Snapshot:** {snap.get('taken_at')}"
           + (f", UniFi Network {snap.get('network_version')}" if snap.get("network_version") else ""),
           "- **Compared with:** " + ("the console after its reset (exact)." if v["exact"] else
                                      "factory defaults. Objects you added are exact; built-in objects and "
                                      "settings show their current values."),
           f"- **Summary:** {s['added']} added, {s['changed']} changed"
           + (f", {s['current']} built-in sections with their current values" if s["current"] else ""), ""]
    sec = None
    for it in v["items"]:
        if it["section"] != sec:
            sec = it["section"]
            out += ["", f"## {TITLES[sec]}", ""]
        mark = {"added": "added", "changed": "changed", "current": "built-in, current values"}[it["kind"]]
        cf = it.get("changed_fields")
        out.append(f"- **{it['label']}**: {mark}" + (f" ({', '.join(cf[:10])})" if cf and it["kind"] == "changed" else ""))
        summ = _summ(it["section"], it["fields"])
        if summ:
            out.append(f"  - {summ}")
        for n in it.get("notes") or []:
            out.append(f"  - {n}")
    if v.get("unavailable"):
        out += ["", "## Not read from this console", ""] + [f"- {TITLES.get(k, k)}: {e}" for k, e in v["unavailable"].items()]
    return "\n".join(out) + "\n"
