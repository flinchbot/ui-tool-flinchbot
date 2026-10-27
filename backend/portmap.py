"""
Switch port map: for every UniFi switch (and gateway with switch ports), what is
plugged into each port, with link speed, PoE draw, errors and the port's settings.

build(devices, clients, networks)  -> model (JSON-friendly, also given to the AI)
render_svg(model)                  -> one vector "front panel" drawing per device

Data sources (all read-only):
  * stat/device port_table          speed, duplex, PoE, errors, uplink flag, SFP
  * stat/device port_overrides      the port's name, profile and native VLAN
  * stat/device uplink of others    which UniFi device hangs off which port
  * stat/device lldp_table          neighbours that speak LLDP (other brands too)
  * stat/sta sw_mac / sw_port       wired clients per port
"""

from datetime import datetime
from html import escape

ERR_WARN = 1          # any rx/tx error since the counters were reset is worth showing
ERR_BAD = 1000        # this many is a real problem (cable, connector, duplex mismatch)
SPEEDS = [(10000, "10G"), (5000, "5G"), (2500, "2.5G"), (1000, "1G"), (100, "100M"), (10, "10M")]


def speed_label(mbps):
    try:
        mbps = int(mbps or 0)
    except (TypeError, ValueError):
        return ""
    for v, label in SPEEDS:
        if mbps >= v:
            return label if mbps < 20000 else f"{mbps // 1000}G"
    return ""


def _name(o):
    return o.get("name") or o.get("hostname") or o.get("mac") or "?"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(devices, clients, networks=None):
    nets = {n.get("_id"): n.get("name") for n in networks or [] if isinstance(n, dict)}
    by_mac = {(d.get("mac") or "").lower(): d for d in devices or [] if isinstance(d, dict)}
    # wired clients per (switch mac, port)
    per_port = {}
    for c in clients or []:
        if not isinstance(c, dict) or not c.get("is_wired") or not c.get("sw_mac"):
            continue
        key = (c["sw_mac"].lower(), c.get("sw_port"))
        per_port.setdefault(key, []).append({
            "name": _name(c), "ip": c.get("ip") or "", "mac": c.get("mac") or "",
            "vendor": c.get("oui") or "", "network": c.get("network") or ""})
    # UniFi devices plugged into a port of another
    downstream = {}
    for d in by_mac.values():
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        um, up_port = (up.get("uplink_mac") or "").lower(), up.get("uplink_remote_port")
        if um and up_port is not None:
            downstream.setdefault((um, up_port), []).append({"name": _name(d), "model": d.get("model") or "",
                                                             "mac": d.get("mac") or "", "ip": d.get("ip") or "",
                                                             "type": d.get("type") or "",
                                                             "nports": len(d.get("port_table") or [])})
    known = {m: _name(d) for m, d in by_mac.items()}
    known.update({(c.get("mac") or "").lower(): _name(c) for c in clients or [] if isinstance(c, dict) and c.get("mac")})

    out = []
    for mac, d in by_mac.items():
        table = d.get("port_table") or []
        if not table or not isinstance(table, list):
            continue
        overrides = {o.get("port_idx"): o for o in d.get("port_overrides") or [] if isinstance(o, dict)}
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        up_port, up_mac = up.get("port_idx"), (up.get("uplink_mac") or "").lower()
        lldp = {}
        for n in d.get("lldp_table") or []:
            if isinstance(n, dict) and n.get("local_port_idx") is not None:
                cid = str(n.get("chassis_id") or "").lower()
                lldp.setdefault(n["local_port_idx"], []).append(
                    {"name": n.get("system_name") or known.get(cid) or n.get("chassis_descr") or cid or "LLDP neighbour",
                     "port": n.get("port_id") or "", "mac": cid})
        ports, poe_total, err_ports = [], 0.0, 0
        for p in table:
            if not isinstance(p, dict) or p.get("port_idx") is None:
                continue
            idx = p["port_idx"]
            ov = overrides.get(idx, {})
            is_up_port = idx == up_port and bool(up_mac)
            devs_here = downstream.get((mac, idx), [])
            dev_macs = {x["mac"].lower() for x in devs_here} | ({up_mac} if is_up_port else set())
            rx_err, tx_err = int(p.get("rx_errors") or 0), int(p.get("tx_errors") or 0)
            poe_w = _num(p.get("poe_power"))
            if poe_w:
                poe_total += poe_w
            entry = {
                "idx": idx, "name": ov.get("name") or p.get("name") or f"Port {idx}",
                "up": bool(p.get("up")), "enabled": p.get("enable", True) is not False,
                "speed": p.get("speed") if p.get("up") else None, "speed_label": speed_label(p.get("speed")) if p.get("up") else "",
                "full_duplex": p.get("full_duplex"), "media": p.get("media") or "",
                "sfp": bool(p.get("sfp_found") or str(p.get("media") or "").upper().startswith("SFP")),
                "is_uplink": bool(p.get("is_uplink")) or is_up_port,
                "uplink_to": (known.get(up_mac) or up_mac) if is_up_port else "",
                "poe_capable": bool(p.get("port_poe")), "poe_mode": p.get("poe_mode") or "",
                "poe_watts": round(poe_w, 1) if poe_w else 0, "poe_class": p.get("poe_class") or "",
                "rx_errors": rx_err, "tx_errors": tx_err, "rx_dropped": int(p.get("rx_dropped") or 0),
                "stp_state": p.get("stp_state") or "",
                "profile": nets.get(ov.get("native_networkconf_id")) or ov.get("op_mode") or "",
                "op_mode": ov.get("op_mode") or p.get("op_mode") or "",
                "aggregated": bool(p.get("aggregated_by")) or p.get("op_mode") == "aggregate",
                "devices": devs_here,
                "clients": sorted(per_port.get((mac, idx), []), key=lambda c: c["name"].lower()),
                "lldp": [x for x in lldp.get(idx, []) if x["mac"] not in dev_macs],   # not repeating UniFi gear
                "issues": [],
            }
            if rx_err + tx_err >= ERR_WARN:
                entry["issues"].append(f"{rx_err + tx_err:,} errors")
                entry["severity"] = "bad" if rx_err + tx_err >= ERR_BAD else "warn"
                err_ports += 1
            if entry["up"] and entry["full_duplex"] is False:
                entry["issues"].append("half duplex")
                entry["severity"] = "bad"
            # A switch or AP on a 100M link is held back (a UPS or similar on 100M is normal).
            slow_peer = [x for x in devs_here if x["type"] == "uap" or x["nports"] > 2]
            if entry["up"] and p.get("speed") and int(p.get("speed") or 0) < 1000 and slow_peer:
                entry["issues"].append(f"{slow_peer[0]['name']} is on a {entry['speed_label']} link")
                entry.setdefault("severity", "warn")
            if entry["stp_state"] in ("blocking", "discarding"):
                entry["issues"].append(f"STP {entry['stp_state']}")
                entry.setdefault("severity", "warn")
            ports.append(entry)
        ports.sort(key=lambda x: x["idx"])
        budget = _num(d.get("total_max_power"))
        out.append({
            "name": _name(d), "model": d.get("model") or "", "type": d.get("type") or "", "mac": d.get("mac") or "",
            "ip": d.get("ip") or "", "online": d.get("state") == 1,
            "ports": ports, "ports_up": sum(1 for p in ports if p["up"]),
            "poe_watts": round(poe_total, 1), "poe_budget": budget,
            "error_ports": err_ports,
        })
    out.sort(key=lambda d: (d["type"] not in ("udm", "ugw", "uxg"), d["name"].lower()))
    return {"devices": out, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def summary_for_ai(model):
    """Compact version for the model: drop empty fields."""
    def slim(p):
        return {k: v for k, v in p.items() if v not in (None, "", [], False, 0) or k in ("idx", "up")}
    return {"devices": [dict({k: v for k, v in d.items() if k != "ports"},
                             ports=[slim(p) for p in d["ports"]]) for d in model["devices"]]}


# ── SVG ───────────────────────────────────────────────────────────────────────
C = {"bg": "#0a0c10", "panel": "#161922", "line": "#2a2f3d", "text": "#e2e8f0", "muted": "#8b95a7",
     "down": "#232836", "bad": "#ff4d6d", "warn": "#ffb020", "poe": "#ffd84d"}
# One hue, darker to brighter = slower to faster (a magnitude, so a sequential ramp).
SPEED_FILL = {"10M": "#173640", "100M": "#1f4e5f", "1G": "#1b7f99", "2.5G": "#12a9cc", "5G": "#00c2eb", "10G": "#00d4ff"}
FONT = "Inter, Segoe UI, system-ui, sans-serif"
MONO = "JetBrains Mono, Consolas, monospace"
PW, PH, GAP = 38, 30, 6          # port box size and gap


def _e(s):
    return escape(str(s), quote=True)


def _tip(dev, p):
    lines = [f"{dev['name']} · {p['name']} (port {p['idx']})"]
    if p["up"]:
        lines.append(f"Link {p['speed_label'] or '?'}{'' if p['full_duplex'] is not False else ', HALF duplex'}"
                     f"{' · SFP ' + p['media'] if p['sfp'] else ''}{' · uplink' if p['is_uplink'] else ''}")
    else:
        lines.append("No link" + ("" if p["enabled"] else " (disabled)"))
    if p["poe_watts"]:
        lines.append(f"PoE {p['poe_watts']} W ({p['poe_mode']})")
    for x in p["devices"]:
        lines.append(f"UniFi: {x['name']} {x['model']} {x['ip']}")
    for x in p["lldp"]:
        lines.append(f"LLDP: {x['name']} {x['port']}")
    for c in p["clients"][:12]:
        lines.append(f"{c['name']}  {c['ip']}  {c['mac']}")
    if len(p["clients"]) > 12:
        lines.append(f"… and {len(p['clients']) - 12} more")
    if p["profile"]:
        lines.append(f"Profile / native network: {p['profile']}")
    if p["issues"]:
        lines.append("Issues: " + ", ".join(p["issues"]))
    return "\n".join(lines)


def _label_for(p):
    if p["uplink_to"] and not p["devices"]:
        return f"↑ {p['uplink_to']}"
    if p["devices"]:
        return p["devices"][0]["name"]
    if p["lldp"]:
        return p["lldp"][0]["name"]
    if p["clients"]:
        n = len(p["clients"])
        return p["clients"][0]["name"] + (f" +{n - 1}" if n > 1 else "")
    return ""


def render_svg(model):
    devs = model["devices"]
    width = 40 + max([((len(d["ports"]) + 1) // 2) * (PW + GAP) for d in devs] + [520]) + 260
    blocks, y = [], 24
    for d in devs:
        ports = d["ports"]
        two_rows = len(ports) > 10
        cols = (len(ports) + 1) // 2 if two_rows else len(ports)
        panel_w = cols * (PW + GAP) + GAP + 20
        panel_h = (2 if two_rows else 1) * (PH + GAP + 14) + GAP + 10
        hdr = (f'<text x="20" y="{y + 14}" font-size="14" font-weight="700" fill="{C["text"]}">{_e(d["name"])}</text>'
               f'<text x="20" y="{y + 30}" font-size="11" font-family="{MONO}" fill="{C["muted"]}">'
               f'{_e(d["model"])} · {_e(d["ip"])} · {d["ports_up"]}/{len(ports)} up'
               + (f' · PoE {d["poe_watts"]} W' + (f' of {d["poe_budget"]:.0f} W' if d["poe_budget"] else '') if d["poe_watts"] else '')
               + (f' · {d["error_ports"]} port(s) with errors' if d["error_ports"] else '') + '</text>')
        py = y + 40
        parts = [hdr, f'<rect x="20" y="{py}" width="{panel_w}" height="{panel_h}" rx="8" fill="{C["panel"]}" stroke="{C["line"]}"/>']
        side = []
        for n, p in enumerate(ports):
            if two_rows:          # odd ports on top, even below, like the real front panel
                col, row = n // 2, n % 2
            else:
                col, row = n, 0
            x = 20 + GAP + 10 + col * (PW + GAP)
            ty = py + GAP + 6 + row * (PH + GAP + 14)
            fill = SPEED_FILL.get(p["speed_label"], C["down"]) if p["up"] else C["down"]
            stroke = {"bad": C["bad"], "warn": C["warn"]}.get(p.get("severity"), C["line"])
            sw = 2.5 if p["issues"] else 1
            dash = "" if p["enabled"] else ' stroke-dasharray="3 3"'          # disabled port
            parts.append(f'<g><title>{_e(_tip(d, p))}</title>'
                         f'<rect x="{x}" y="{ty}" width="{PW}" height="{PH}" rx="{3 if not p["sfp"] else 1}" fill="{fill}" '
                         f'stroke="{stroke}" stroke-width="{sw}"{dash}/>'
                         f'<text x="{x + PW / 2}" y="{ty + 13}" text-anchor="middle" font-size="10" font-family="{MONO}" '
                         f'fill="{C["text"] if p["up"] else C["muted"]}">{p["idx"]}</text>'
                         f'<text x="{x + PW / 2}" y="{ty + 25}" text-anchor="middle" font-size="8.5" font-family="{MONO}" '
                         f'fill="{C["text"] if p["up"] else C["muted"]}">{_e(p["speed_label"] or ("off" if not p["enabled"] else ""))}</text>'
                         + (f'<text x="{x + PW - 3}" y="{ty + 9}" text-anchor="end" font-size="8" fill="{C["poe"]}">⚡</text>' if p["poe_watts"] else '')
                         + (f'<text x="{x + 3}" y="{ty + 9}" font-size="8" fill="{C["warn"]}">↑</text>' if p["is_uplink"] else '')
                         + (f'<text x="{x + PW / 2}" y="{ty + PH + 11}" text-anchor="middle" font-size="8" font-family="{MONO}" '
                            f'fill="{C["poe"]}">{p["poe_watts"]:g}W</text>' if p["poe_watts"] else '')
                         + '</g>')
            lab = _label_for(p)
            if lab or p["issues"]:
                side.append((p, lab))
        # list of what's on each port, to the right of the panel
        lx, ly = 20 + panel_w + 24, py + 10
        for p, lab in side:
            color = {"bad": C["bad"], "warn": C["warn"]}.get(p.get("severity"), C["text"])
            parts.append(f'<text x="{lx}" y="{ly}" font-size="10.5" font-family="{MONO}" fill="{C["muted"]}">{p["idx"]:>2}</text>'
                         f'<text x="{lx + 24}" y="{ly}" font-size="10.5" fill="{color}">{_e(lab[:38] or "-")}'
                         + (f' <tspan fill="{C["bad"] if color == C["bad"] else C["warn"]}">({_e(", ".join(p["issues"]))})</tspan>' if p["issues"] else '')
                         + '</text>')
            ly += 14
        height = max(panel_h + 40, ly - y + 4)
        blocks.append("".join(parts))
        y += height + 28
    legend_y = y
    legend = [f'<text x="20" y="{legend_y}" font-size="10.5" fill="{C["muted"]}">Link speed:</text>']
    lx = 90
    for label in ("100M", "1G", "2.5G", "10G"):
        legend.append(f'<rect x="{lx}" y="{legend_y - 10}" width="14" height="12" rx="2" fill="{SPEED_FILL[label]}"/>'
                      f'<text x="{lx + 18}" y="{legend_y}" font-size="10.5" fill="{C["muted"]}">{label}</text>')
        lx += 62
    legend.append(f'<rect x="{lx}" y="{legend_y - 10}" width="14" height="12" rx="2" fill="{C["down"]}" stroke="{C["line"]}"/>'
                  f'<text x="{lx + 18}" y="{legend_y}" font-size="10.5" fill="{C["muted"]}">no link</text>'
                  f'<text x="{lx + 80}" y="{legend_y}" font-size="10.5" fill="{C["poe"]}">⚡ PoE</text>'
                  f'<text x="{lx + 130}" y="{legend_y}" font-size="10.5" fill="{C["warn"]}">↑ uplink</text>'
                  f'<rect x="{lx + 192}" y="{legend_y - 10}" width="14" height="12" rx="2" fill="none" stroke="{C["bad"]}" stroke-width="2"/>'
                  f'<text x="{lx + 210}" y="{legend_y}" font-size="10.5" fill="{C["muted"]}">serious (≥{ERR_BAD:,} errors, half duplex)</text>'
                  f'<rect x="{lx + 430}" y="{legend_y - 10}" width="14" height="12" rx="2" fill="none" stroke="{C["warn"]}" stroke-width="2"/>'
                  f'<text x="{lx + 448}" y="{legend_y}" font-size="10.5" fill="{C["muted"]}">worth a look</text>')
    height = legend_y + 20
    width = max(width, lx + 448 + 110)          # room for the whole legend
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'font-family="{FONT}"><rect width="100%" height="100%" fill="{C["bg"]}"/>' + "".join(blocks) + "".join(legend) + "</svg>")
