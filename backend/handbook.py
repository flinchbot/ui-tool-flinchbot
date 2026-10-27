"""
Network handbook: one printable HTML page documenting the whole network - for a
handover, an insurance file, or future you. Open it in any browser; Print → Save
as PDF makes a PDF.

Everything comes from the console's own data (read-only). Wi-Fi passwords are
left out unless the user asks for them.
"""

from datetime import datetime
from html import escape

import portmap
import topology


def _e(v):
    return escape("" if v is None else str(v), quote=True)


def _table(headers, rows, cls=""):
    if not rows:
        return '<p class="muted">None.</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'


def _uptime(sec):
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return ""
    d, s = divmod(s, 86400)
    h = s // 3600
    return f"{d} d {h} h" if d else f"{h} h"


CSS = """
:root { --text:#1b1f27; --muted:#5d6675; --line:#d9dee7; --accent:#0a6fb8; --bad:#c22f45; --warn:#9a6400; --bg:#ffffff; --soft:#f4f6f9; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --text:#e2e8f0; --muted:#97a1b3; --line:#2c3342; --accent:#4fb6ff; --bad:#ff6b81; --warn:#ffbe4d; --bg:#0f1218; --soft:#171b24; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 Inter, "Segoe UI", system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:32px 24px 64px; }
h1 { font-size:28px; margin:0 0 4px; } h2 { font-size:20px; margin:36px 0 10px; padding-top:12px; border-top:2px solid var(--line); }
h3 { font-size:15px; margin:20px 0 6px; }
.muted { color:var(--muted); } .bad { color:var(--bad); font-weight:600; } .warn { color:var(--warn); }
table { width:100%; border-collapse:collapse; margin:6px 0 12px; font-size:12.5px; }
th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { background:var(--soft); font-weight:600; } td code, .mono { font-family:"JetBrains Mono", Consolas, monospace; font-size:12px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:14px 0; }
.card { background:var(--soft); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.card b { display:block; font-size:20px; } .card span { color:var(--muted); font-size:12px; }
nav ol { columns:2; font-size:13px; } nav a { color:var(--accent); text-decoration:none; }
.svg { overflow:auto; border:1px solid var(--line); border-radius:8px; margin:8px 0; } .svg svg { display:block; max-width:100%; height:auto; }
.pw { font-family:"JetBrains Mono", Consolas, monospace; background:var(--soft); padding:1px 5px; border-radius:4px; }
@media print { body { font-size:11px; } main { padding:0; max-width:none; } h2 { break-before:page; border:0; } h2:first-of-type { break-before:auto; }
  .svg { break-inside:avoid; } table { font-size:10px; } nav { break-after:page; } }
@media (max-width:640px) { nav ol { columns:1; } main { padding:16px; } }
"""


def build(data, network, host, include_passwords=False, changes=None, app_version=""):
    """data: devices, clients, networks, wlans, settings, port_forwards, policies, zones,
    routes, traffic_routes, users, sysinfo, health. Returns an HTML string."""
    now = datetime.now().astimezone()
    devices = [d for d in data.get("devices") or [] if isinstance(d, dict)]
    clients = [c for c in data.get("clients") or [] if isinstance(c, dict)]
    nets = [n for n in data.get("networks") or [] if isinstance(n, dict)]
    wlans = [w for w in data.get("wlans") or [] if isinstance(w, dict)]
    netname = {n.get("_id"): n.get("name") for n in nets}
    zones = {z.get("_id"): z.get("name") for z in data.get("zones") or [] if isinstance(z, dict)}
    info = (data.get("sysinfo") or [{}])
    info = info[0] if isinstance(info, list) and info else info if isinstance(info, dict) else {}
    S = {s.get("key"): s for s in data.get("settings") or [] if isinstance(s, dict)}
    sections = []

    def section(sid, title, body):
        sections.append((sid, title, f'<h2 id="{sid}">{_e(title)}</h2>{body}'))

    # overview
    wan = next((h for h in data.get("health") or [] if isinstance(h, dict) and h.get("subsystem") == "wan"), {})
    cards = [(len(devices), "UniFi devices"), (len(clients), "clients online"),
             (sum(1 for n in nets if n.get("purpose") in ("corporate", "guest")), "LANs / VLANs"),
             (len(wlans), "Wi-Fi networks"), (wan.get("wan_ip") or "?", "WAN address"),
             (info.get("version") or "?", "UniFi Network")]
    over = '<div class="cards">' + "".join(f'<div class="card"><b>{_e(v)}</b><span>{_e(l)}</span></div>' for v, l in cards) + "</div>"
    country = (S.get("country") or {}).get("code")
    tz = (S.get("locale") or {}).get("timezone")
    over += _table(["Item", "Value"], [
        ["Console", _e(f"{info.get('name') or info.get('hostname') or host} ({info.get('ubnt_device_type') or info.get('udm_version') or ''})")],
        ["Console address", _e(host)],
        ["ISP", _e(wan.get("isp_name") or wan.get("isp_organization") or "")],
        ["Gateway", _e(wan.get("gw_name") or "")],
        ["Time zone", _e(tz or "")], ["Country code", _e(country or "")],
        ["Intrusion prevention", _e((S.get("ips") or {}).get("ips_mode") or "")],
        ["UPnP", "on" if (S.get("usg") or {}).get("upnp_enabled") else "off"],
    ])
    section("overview", "Overview", over)

    # networks
    rows = []
    for n in nets:
        if n.get("purpose") in ("corporate", "guest", "vlan-only"):
            dhcp = f"{n.get('dhcpd_start')} – {n.get('dhcpd_stop')}" if n.get("dhcpd_enabled") else ("relay" if n.get("dhcp_relay_enabled") else "off")
            dns = ", ".join(x for x in (n.get("dhcpd_dns_1"), n.get("dhcpd_dns_2")) if x) if n.get("dhcpd_dns_enabled") else "gateway"
            rows.append([_e(n.get("name")), _e(n.get("purpose")), _e(n.get("vlan") if n.get("vlan_enabled") or n.get("vlan") else "untagged"),
                         f"<code>{_e(n.get('ip_subnet') or '')}</code>", _e(dhcp), _e(dns), _e(n.get("domain_name") or ""),
                         _e(zones.get(n.get("firewall_zone_id")) or "")])
    body = _table(["Name", "Purpose", "VLAN", "Gateway / subnet", "DHCP range", "DNS handed out", "Domain", "Firewall zone"], rows)
    wans = [n for n in nets if n.get("purpose") == "wan"]
    if wans:
        body += "<h3>Internet (WAN)</h3>" + _table(["Name", "Type", "VLAN", "Zone"], [
            [_e(n.get("name")), _e(n.get("wan_type")), _e(n.get("wan_vlan") if n.get("wan_vlan_enabled") else "—"),
             _e(zones.get(n.get("firewall_zone_id")) or "")] for n in wans])
    vpns = [n for n in nets if n.get("purpose") in ("vpn-client", "remote-user-vpn", "site-vpn")]
    if vpns:
        body += "<h3>VPN</h3>" + _table(["Name", "Kind", "Type", "Address", "Enabled"], [
            [_e(n.get("name")), _e(n.get("purpose")), _e(n.get("vpn_type")), f"<code>{_e(n.get('ip_subnet') or '')}</code>",
             "yes" if n.get("enabled", True) else "no"] for n in vpns])
    section("networks", "Networks and VLANs", body)

    # wi-fi
    rows = []
    for w in wlans:
        sec = (w.get("security") or "").upper()
        if w.get("wpa_mode"):
            sec += f" {w['wpa_mode'].upper()}" + ("/WPA3" if w.get("wpa3_transition") else "")
        pw = ""
        if include_passwords and w.get("x_passphrase"):
            pw = f'<span class="pw">{_e(w["x_passphrase"])}</span>'
        rows.append([_e(w.get("name")), _e(sec), _e("/".join(w.get("wlan_bands") or []) or "all"),
                     _e(netname.get(w.get("networkconf_id")) or ""), "hidden" if w.get("hide_ssid") else "",
                     "guest" if w.get("is_guest") else "", "on" if w.get("enabled", True) else "off"]
                    + ([pw] if include_passwords else []))
    section("wifi", "Wi-Fi", _table(["SSID", "Security", "Bands", "Network", "", "", "State"]
                                    + (["Password"] if include_passwords else []), rows)
            + ("" if include_passwords else '<p class="muted">Passwords are not included in this copy.</p>'))

    # devices
    rows = []
    for d in sorted(devices, key=lambda d: (d.get("type") != "udm", d.get("name") or "")):
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        parent = next((x.get("name") for x in devices if (x.get("mac") or "").lower() == (up.get("uplink_mac") or "").lower()), "")
        rows.append([_e(d.get("name") or d.get("mac")), _e(d.get("model")), f"<code>{_e(d.get('ip') or '')}</code>",
                     f"<code>{_e(d.get('mac'))}</code>", _e(d.get("version")) + (' <span class="warn">update</span>' if d.get("upgradable") else ""),
                     _e(f"{parent} port {up.get('uplink_remote_port')}" if parent else ("wireless mesh" if up.get("type") == "wireless" else "")),
                     _e(_uptime(d.get("uptime"))), "online" if d.get("state") == 1 else '<span class="bad">offline</span>'])
    section("devices", "UniFi devices", _table(["Name", "Model", "IP", "MAC", "Firmware", "Uplink", "Uptime", "State"], rows))

    # topology + ports
    try:
        topo = topology.render_svg(topology.build_model(devices, clients, None, network=network))
        section("topology", "Topology", f'<div class="svg">{topo}</div><p class="muted">Clients are drawn only when they have a problem.</p>')
    except Exception as e:                                           # a drawing must not stop the handbook
        section("topology", "Topology", f'<p class="muted">Not available ({_e(e)}).</p>')
    pm = portmap.build(devices, clients, nets)
    body = f'<div class="svg">{portmap.render_svg(pm)}</div>' if pm["devices"] else ""
    for d in pm["devices"]:
        rows = []
        for p in d["ports"]:
            what = "; ".join([f"{x['name']} ({x['model']})" for x in p["devices"]] + [f"{x['name']}" for x in p["lldp"]]
                             + [f"{c['name']} {c['ip']}" for c in p["clients"][:6]]
                             + ([f"+{len(p['clients']) - 6} more"] if len(p["clients"]) > 6 else []))
            rows.append([str(p["idx"]), _e(p["name"]), _e(p["speed_label"] or ("off" if not p["enabled"] else "—")),
                         _e(f"{p['poe_watts']} W" if p["poe_watts"] else ""), _e(p["profile"]), _e(what),
                         f'<span class="{"bad" if p.get("severity") == "bad" else "warn"}">{_e(", ".join(p["issues"]))}</span>' if p["issues"] else ""])
        body += f"<h3>{_e(d['name'])} <span class=\"muted\">{_e(d['model'])} · {_e(d['ip'])}</span></h3>" + \
            _table(["Port", "Name", "Link", "PoE", "Profile / native network", "Connected", "Issues"], rows)
    section("ports", "Switch ports", body or '<p class="muted">No switches.</p>')

    # firewall
    pol = [x for x in data.get("policies") or [] if isinstance(x, dict) and not x.get("predefined")]

    def target(t):
        t = t or {}
        mt = t.get("matching_target") or "ANY"
        extra = t.get("ips") or t.get("client_macs") or t.get("regions") or [netname.get(n, n) for n in t.get("network_ids") or []]
        port = f" port {t['port']}" if t.get("port") else ""
        return f"{zones.get(t.get('zone_id'), '?')}: {mt}{' ' + ', '.join(map(str, extra))[:200] if extra else ''}{port}"
    body = _table(["Name", "Action", "Protocol", "From", "To", "On"], [
        [_e(x.get("name")), _e(x.get("action")), _e(x.get("protocol")), _e(target(x.get("source"))),
         _e(target(x.get("destination"))), "yes" if x.get("enabled", True) else "no"]
        for x in sorted(pol, key=lambda x: x.get("index") or 0)])
    pf = [x for x in data.get("port_forwards") or [] if isinstance(x, dict)]
    body += "<h3>Port forwards</h3>" + _table(["Name", "WAN port", "To", "Protocol", "Source", "On"], [
        [_e(x.get("name")), _e(x.get("dst_port")), f"<code>{_e(x.get('fwd'))}:{_e(x.get('fwd_port'))}</code>",
         _e(x.get("proto")), _e(x.get("src") or "any"), "yes" if x.get("enabled", True) else "no"] for x in pf])
    tr = [x for x in data.get("traffic_routes") or [] if isinstance(x, dict)]
    if tr:
        body += "<h3>Policy-based routes</h3>" + _table(["Description", "Matches", "Via", "Kill switch", "On"], [
            [_e(x.get("description")), _e(x.get("matching_target")), _e(netname.get(x.get("network_id")) or x.get("next_hop") or ""),
             "yes" if x.get("kill_switch_enabled") else "no", "yes" if x.get("enabled", True) else "no"] for x in tr])
    rt = [x for x in data.get("routes") or [] if isinstance(x, dict)]
    if rt:
        body += "<h3>Static routes</h3>" + _table(["Name", "Network", "Next hop", "On"], [
            [_e(x.get("name")), f"<code>{_e(x.get('static-route_network'))}</code>",
             _e(x.get("static-route_nexthop") or x.get("static-route_interface")), "yes" if x.get("enabled", True) else "no"] for x in rt])
    section("firewall", "Firewall, port forwards and routing", body)

    # reserved addresses
    users = [u for u in data.get("users") or [] if isinstance(u, dict) and (u.get("use_fixedip") or u.get("name"))]
    rows = [[_e(u.get("name") or u.get("hostname") or ""), f"<code>{_e(u.get('mac'))}</code>",
             f"<code>{_e(u.get('fixed_ip'))}</code>" if u.get("use_fixedip") else "",
             _e(netname.get(u.get("network_id")) or "" if u.get("use_fixedip") else ""), _e(u.get("note") or "")]
            for u in sorted(users, key=lambda u: tuple(int(x) if x.isdigit() else 0 for x in str(u.get("fixed_ip") or "999").split(".")))]
    section("clients", "Named clients and fixed IPs", _table(["Name", "MAC", "Fixed IP", "Network", "Note"], rows))

    # current clients by network
    by_net = {}
    for c in clients:
        by_net.setdefault(c.get("network") or c.get("essid") or "?", []).append(c)
    section("online", "Clients online now", _table(["Network", "Clients", "Wired", "Wireless"], [
        [_e(k), str(len(v)), str(sum(1 for c in v if c.get("is_wired"))), str(sum(1 for c in v if not c.get("is_wired")))]
        for k, v in sorted(by_net.items())]))

    if changes:
        section("changes", "Recent changes made through this app", _table(["When", "What", "Target", "Result", "By"], [
            [_e(c.get("at", "")[:16].replace("T", " ")), _e(c.get("label")), _e(c.get("target") or ""),
             _e("ok" if c.get("ok") else ("denied" if c.get("decision") == "denied" else "failed")), _e(c.get("user") or "")]
            for c in changes[:40]]))

    nav = "<nav><h2 style=\"border:0;margin-top:20px\">Contents</h2><ol>" + "".join(
        f'<li><a href="#{sid}">{_e(t)}</a></li>' for sid, t, _ in sections) + "</ol></nav>"
    head = (f"<h1>{_e(network)} network handbook</h1><p class=\"muted\">Generated {now.strftime('%d %B %Y, %H:%M')} from "
            f"{_e(host)} by UniFi × flinchbot {app_version}. Live data at that moment; settings change over time."
            + (" <b class=\"bad\">Contains Wi-Fi passwords: keep it private.</b>" if include_passwords else "") + "</p>")
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width, initial-scale=1\"><title>{_e(network)} network handbook</title>"
            f"<style>{CSS}</style></head><body><main>{head}{nav}" + "".join(b for _, _, b in sections) + "</main></body></html>")
