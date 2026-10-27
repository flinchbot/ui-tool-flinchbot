"""
Undo for approved Wi-Fi / LAN changes.

Before an approved change runs, prepare() snapshots what it is about to change;
undo() puts it back later. Supported:

  update_wlan / update_network   the previous values of exactly the fields changed
  delete_wlan                    the whole Wi-Fi network, recreated (it gets a new id)
  create_wlan / create_network   undo = delete what was created

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
}
# Server-managed fields that must not be sent back when recreating an object.
_SERVER_FIELDS = {"site_id", "external_id"}


def is_secret_key(k):
    kl = str(k).lower()
    return (kl.startswith("x_") or kl.endswith("_key")
            or any(s in kl for s in ("passphrase", "password", "secret", "token", "psk")))


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


def prepare(client, tool, inp):
    """Snapshot before an approved change. -> dict (may hold secrets) or None."""
    if tool not in ROLLBACK_TOOLS:
        return None
    kind, obj_type, id_key = ROLLBACK_TOOLS[tool]
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
    """After the change ran: record what a create produced. -> prep or None (nothing to undo)."""
    if not prep:
        return None
    if prep["kind"] == "delete_created":
        created = _first(result_obj) or {}
        if not created.get("_id"):
            return None
        prep["object_id"] = created["_id"]
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

    if kind == "update":
        try:
            current = client.get_object(obj_type, oid)
        except UniFiError as e:
            if e.kind == "notfound":
                raise UniFiError(f"The {what} “{full.get('name')}” no longer exists.", "rollback")
            raise
        # Changed again since? Compare the fields we set (secrets can't be compared).
        changed = {k: current.get(k) for k, v in full.get("after", {}).items()
                   if not is_secret_key(k) and k in full.get("before", {}) and current.get(k) != v}
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

    # delete_created
    try:
        client.delete_object(obj_type, oid)
    except UniFiError as e:
        if e.kind != "notfound":
            raise
        return {"ok": True, "detail": f"The {what} “{full.get('name')}” was already gone."}
    return {"ok": True, "detail": f"Deleted the {what} “{full.get('name')}”."}
