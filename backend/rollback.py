"""
Undo for approved Wi-Fi / LAN changes.

Before an approved change runs, prepare() snapshots what it is about to change;
undo() puts it back later. Supported:

  update_wlan / update_network   the previous values of exactly the fields changed
  delete_wlan                    the whole Wi-Fi network, recreated (it gets a new id)
  create_wlan / create_network   undo = delete what was created
  set_switch_port                the previous values of the port settings changed
  set_client_name                the client's previous name (and note)

Undo never silently overwrites a newer change: each snapshot also keeps what the
change set ("after"), and if any of those fields (passwords included, compared
here and never shown) were changed again since, or a created network was edited
since, undo returns a conflict and only goes ahead when the user confirms. Only
the fields the original change owned are put back.

Secrets never go into the log file. Any secret value in a snapshot (an old
Wi-Fi passphrase, a RADIUS secret...) is split out and stored like the
other passwords (Windows Credential Manager or the macOS Keychain, or
data/secrets.json as the fallback) under "rb-<record id>", and joined back
only when undo runs.
"""

import json

import config_store as cfg
from unifi_client import UniFiError

SECRET = "<stored separately>"
ROLLBACK_TOOLS = {
    "update_wlan":    ("update", "wlanconf", "wlan_id"),
    "update_network": ("update", "networkconf", "network_id"),
    "delete_wlan":    ("recreate", "wlanconf", "wlan_id"),
    "create_wlan":    ("delete_created", "wlanconf", None),
    "create_network": ("delete_created", "networkconf", None),
    "set_switch_port": ("port_override", "device", "mac"),
    "set_client_name": ("client_fields", "user", "mac"),
}
# Server-managed fields that must not be sent back when recreating an object.
_SERVER_FIELDS = {"site_id", "external_id"}


# Field-name fragments that mean "this value is a secret". UniFi marks most secrets
# with an x_ prefix, but not all: a WireGuard VPN client keeps its whole config file,
# private key included, in wireguard_client_configuration_file, and Wi-Fi private
# pre-shared keys live in private_preshared_keys.
_SECRET_PARTS = ("passphrase", "password", "secret", "token", "psk", "private", "preshared",
                 "configuration_file", "openvpn_configuration", "_keys")


# ...and names that look like secrets but aren't: a firewall zone's built-in name,
# public keys, and file names.
_NOT_SECRET = ("zone_key", "public_key", "_filename", "publickey")


def is_secret_key(k):
    kl = str(k).lower()
    if any(s in kl for s in _NOT_SECRET) and not kl.startswith("x_"):
        return False
    return (kl.startswith("x_") or kl.endswith("_key")
            or any(s in kl for s in _SECRET_PARTS))


def split_secrets(obj):
    """-> (copy with secret values replaced by SECRET, {json path: value})."""
    secrets = {}

    def walk(o, path):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if is_secret_key(k) and v not in (None, "", [], {}):
                    secrets[json.dumps(path + [k])] = v
                    out[k] = SECRET
                else:
                    out[k] = walk(v, path + [k])
            return out
        if isinstance(o, list):
            return [walk(v, path + [i]) for i, v in enumerate(o)]
        return o
    return walk(obj, []), secrets


def join_secrets(obj, secrets):
    obj = json.loads(json.dumps(obj))
    for p, v in (secrets or {}).items():
        path, cur = json.loads(p), obj
        try:
            for step in path[:-1]:
                cur = cur[step]
            cur[path[-1]] = v
        except (KeyError, IndexError, TypeError):
            pass
    return obj


def mask(obj):
    """For display and the log: secret values hidden at any depth."""
    return split_secrets(obj)[0]


def _first(resp):
    data = resp.get("data") if isinstance(resp, dict) else resp
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return data if isinstance(data, dict) else None


def _port_owned(inp):
    """The port-override fields a set_switch_port call changes."""
    return ([k for k in ("name", "poe_mode") if inp.get(k) is not None]
            + (["forward"] if inp.get("enabled") is not None else []))


def _client_view(u):
    return {"name": u.get("name") or "", "note": u.get("note") or "", "noted": bool(u.get("noted"))}


def prepare(client, tool, inp):
    """Snapshot before an approved change. -> dict (may hold secrets) or None."""
    if tool not in ROLLBACK_TOOLS:
        return None
    kind, obj_type, id_key = ROLLBACK_TOOLS[tool]
    if kind == "port_override":
        d = client.device_record(inp["mac"])
        idx = int(inp["port_idx"])
        before = next((dict(o) for o in d.get("port_overrides") or [] if isinstance(o, dict) and o.get("port_idx") == idx), None)
        return {"kind": kind, "object": obj_type, "mac": inp["mac"], "port_idx": idx,
                "name": f"{d.get('name') or inp['mac']} port {idx}", "before": before, "owned": _port_owned(inp)}
    if kind == "client_fields":
        u = client.client_record(inp["mac"])
        owned = ["name"] + (["note", "noted"] if inp.get("note") is not None else [])
        after = {"name": str(inp.get("name") or "").strip()}
        if inp.get("note") is not None:
            after.update(note=inp["note"], noted=bool(inp["note"]))
        return {"kind": kind, "object": obj_type, "mac": inp["mac"], "object_id": u["_id"],
                "name": u.get("name") or u.get("hostname") or inp["mac"],
                "before": _client_view(u), "owned": owned, "after": after}
    if kind == "delete_created":
        return {"kind": kind, "object": obj_type, "name": inp.get("name")}
    current = client.get_object(obj_type, inp[id_key])
    if kind == "recreate":
        return {"kind": kind, "object": obj_type, "object_id": inp[id_key], "name": current.get("name"),
                "before": current}
    settings = inp.get("settings") if isinstance(inp.get("settings"), dict) else {}
    return {"kind": kind, "object": obj_type, "object_id": inp[id_key], "name": current.get("name"),
            "before": {k: current[k] for k in settings if k in current},
            "absent": [k for k in settings if k not in current],
            "after": dict(settings)}


def finish(prep, result_obj):
    """After the change ran: record what it produced (the created object, the port's new
    settings), so undo can tell whether anyone changed it since. -> prep or None (nothing to undo)."""
    if not prep:
        return None
    if prep["kind"] == "delete_created":
        created = _first(result_obj) or {}
        if not created.get("_id"):
            return None
        prep["object_id"] = created["_id"]
        prep["created"] = {k: v for k, v in created.items() if not k.startswith("_") and k not in _SERVER_FIELDS}
    if prep["kind"] == "port_override" and isinstance(result_obj, dict):
        now = result_obj.get("port_settings") if isinstance(result_obj.get("port_settings"), dict) else {}
        prep["after"] = {k: now.get(k) for k in prep.get("owned") or [] if k in now}
    return prep


def store(prep, record_id):
    """Split secrets out of the snapshot into the secret store. -> public dict for the log."""
    public, secrets = split_secrets(prep)
    if secrets:
        cfg.set_secret(f"rb-{record_id}", json.dumps(secrets))
        public["has_secrets"] = True
    return public


def forget(record_id):
    cfg.delete_secret(f"rb-{record_id}", kind="")


def describe(public):
    """One line for the UI: what an undo would do."""
    k, name = public["kind"], public.get("name") or public.get("object_id")
    if k == "port_override":
        return f"Put back the previous settings of {name}"
    if k == "client_fields":
        return f"Put back the previous name and note of client “{name}”"
    what = "Wi-Fi network" if public["object"] == "wlanconf" else "LAN/VLAN"
    if k == "update":
        fields = ", ".join(sorted(public.get("before", {}))) or "nothing"
        return f"Put back {fields} on {what} “{name}”"
    if k == "recreate":
        return f"Recreate the deleted {what} “{name}”"
    return f"Delete the {what} “{name}” that this change created"


def undo(client, public, record_id, force=False):
    """Apply the undo. -> {"ok": True, "detail"} or {"ok": False, "conflict": True, "detail"}."""
    secrets = {}
    if public.get("has_secrets"):
        raw = cfg.get_secret(f"rb-{record_id}")
        if not raw:
            raise UniFiError("The saved copy of the old password for this change is missing, so it "
                             "can't be undone automatically.", "rollback")
        secrets = json.loads(raw)
    full = join_secrets(public, secrets)
    kind, obj_type, oid = full["kind"], full["object"], full.get("object_id")
    what = "Wi-Fi network" if obj_type == "wlanconf" else "LAN/VLAN"

    if kind == "port_override":
        if "owned" not in full:            # logged by an older version: the whole port override goes back
            client.restore_port_override(full["mac"], full["port_idx"], full.get("before"))
            return {"ok": True, "detail": f"Put back the previous settings of {full.get('name')}."}
        d = client.device_record(full["mac"])
        idx, owned, before = full["port_idx"], full["owned"], full.get("before") or {}
        ovs = [dict(o) for o in d.get("port_overrides") or [] if isinstance(o, dict)]
        cur = next((o for o in ovs if o.get("port_idx") == idx), None)
        after = full.get("after") or {}
        changed = {k: (cur or {}).get(k) for k in owned if k in after and (cur or {}).get(k) != after[k]}
        if changed and not force:
            return {"ok": False, "conflict": True, "changed": mask(changed),
                    "detail": f"These settings of {full.get('name')} were changed again since: "
                              f"{', '.join(sorted(changed))}. Undoing would overwrite that newer change."}
        new = dict(cur or {"port_idx": idx})
        for k in owned:                    # only what this change set; the port's other settings stay
            if k in before:
                new[k] = before[k]
            else:
                new.pop(k, None)
        keep = set(new) - {"port_idx"}
        if cur is not None:
            ovs = [new if o is cur else o for o in ovs] if keep else [o for o in ovs if o is not cur]
        elif keep:
            ovs.append(new)
        client.update_device(d["_id"], {"port_overrides": ovs})
        return {"ok": True, "detail": f"Put back {', '.join(sorted(owned))} on {full.get('name')}."}
    if kind == "client_fields":
        owned = full.get("owned") or list(full["before"])
        after = full.get("after")
        if after and not force:
            now = _client_view(client.client_record(full["mac"]))
            changed = {k: now[k] for k in owned if k in after and now.get(k) != after[k]}
            if changed:
                return {"ok": False, "conflict": True, "changed": mask(changed),
                        "detail": f"The client's {', '.join(sorted(changed))} was changed again since. "
                                  f"Undoing would overwrite that newer change."}
        client.update_client(oid, {k: full["before"][k] for k in owned if k in full["before"]})
        return {"ok": True, "detail": f"Put back the previous name of client “{full['before'].get('name') or full.get('mac')}”."}

    if kind == "update":
        try:
            current = client.get_object(obj_type, oid)
        except UniFiError as e:
            if e.kind == "notfound":
                raise UniFiError(f"The {what} “{full.get('name')}” no longer exists.", "rollback")
            raise
        # Changed again since? Compare the fields we set. Secrets are compared too (here, never
        # shown), when the console returns them: undoing a password change must not quietly
        # replace a newer password.
        changed = {k: current.get(k) for k, v in full.get("after", {}).items()
                   if k in full.get("before", {}) and (k in current or not is_secret_key(k))
                   and current.get(k) != v}
        if changed and not force:
            return {"ok": False, "conflict": True,
                    "detail": f"These settings were changed again since: {', '.join(sorted(changed))}. "
                              f"Undoing would overwrite that newer change.", "changed": mask(changed)}
        client.update_object(obj_type, oid, full["before"])
        note = (f" ({', '.join(full['absent'])} didn't exist before and were left as they are.)"
                if full.get("absent") else "")
        return {"ok": True, "detail": f"Put back {', '.join(sorted(full['before'])) or 'nothing'} on "
                                      f"{what} “{full.get('name')}”.{note}"}

    if kind == "recreate":
        names = {o.get("name") for o in client.list_objects(obj_type)}
        if full.get("name") in names and not force:
            return {"ok": False, "conflict": True,
                    "detail": f"A {what} named “{full['name']}” exists again. Recreating would make a second one."}
        payload = {k: v for k, v in full["before"].items() if not k.startswith("_") and k not in _SERVER_FIELDS}
        created = _first(client.create_object(obj_type, payload)) or {}
        return {"ok": True, "detail": f"Recreated {what} “{full.get('name')}” (new id {created.get('_id', '?')})."}

    # delete_created: only if it is still what this change created
    created = full.get("created")
    if created and not force:
        try:
            current = client.get_object(obj_type, oid)
        except UniFiError as e:
            if e.kind != "notfound":
                raise
            return {"ok": True, "detail": f"The {what} “{full.get('name')}” was already gone."}
        changed = {k: current.get(k) for k, v in created.items() if k in current and current.get(k) != v}
        if changed:
            return {"ok": False, "conflict": True, "changed": mask(changed),
                    "detail": f"The {what} “{full.get('name')}” was changed since it was created "
                              f"({', '.join(sorted(changed))}). Deleting it would throw that work away, and it "
                              f"may be in use now."}
    try:
        client.delete_object(obj_type, oid)
    except UniFiError as e:
        if e.kind != "notfound":
            raise
        return {"ok": True, "detail": f"The {what} “{full.get('name')}” was already gone."}
    return {"ok": True, "detail": f"Deleted the {what} “{full.get('name')}”."}
