"""
Network topology: the UniFi devices as a tree (who is plugged into what, with
port numbers) plus ONLY the clients that currently have a problem, drawn under
the device they are connected to.

Two halves, both pure functions of UniFi data so they are easy to test:
  build_model(devices, clients, events) -> dict   (also what the AI's get_topology tool returns)
  render_svg(model) -> str                        (vector; shown inline in the app and downloadable)

What counts as a problem client (THRESHOLDS below):
  * Wi-Fi: weak signal, high retry rate, low satisfaction
  * wired: errors or half duplex on its switch port, low satisfaction
  * any: flapping - many connect/disconnect/roam events in the last 24 h
Only clients the console currently lists are shown (a client that is offline
right now has nothing to be connected to).
"""

import html
import json
import re
from datetime import datetime

THRESHOLDS = {
    "signal_weak_dbm": -72,     # warn below this...
    "signal_bad_dbm": -80,      # ...red below this
    "retry_pct_warn": 15,       # Wi-Fi tx retries / tx packets
    "retry_pct_bad": 30,
    "min_tx_packets": 500,      # ignore retry rate on clients that barely talked
    "satisfaction": 60,         # UniFi's own 0-100 experience score
    "flaps_24h": 8,             # connect/disconnect/roam events in 24 h
}

GATEWAY_TYPES = ("ugw", "udm", "uxg", "ucg")
_MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)


# ── model ─────────────────────────────────────────────────────────────────────
def count_flaps(events):
    """MAC -> number of connect/disconnect/roam events. Works on the v2 system log
    without assuming its exact shape: any MAC mentioned in such an event counts."""
    counts = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        key = str(ev.get("key") or ev.get("type") or ev.get("category") or "")
        if not re.search(r"CONNECT|DISCONNECT|ROAM", key, re.I):
            continue
        for m in {m.lower() for m in _MAC_RE.findall(json.dumps(ev, default=str))}:
            counts[m] = counts.get(m, 0) + 1
    return counts


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def client_issues(c, port=None, flaps=0, t=THRESHOLDS):
    """-> list of (severity, text); severity is 'warn' or 'bad'. Empty = fine."""
    issues = []
    if not c.get("is_wired"):
        sig = _num(c.get("signal"))
        if sig is None and _num(c.get("rssi")) is not None:
            sig = _num(c.get("rssi")) - 95          # UniFi rssi is positive, roughly dBm + 95
        if sig is not None and sig <= t["signal_bad_dbm"]:
            issues.append(("bad", f"very weak signal ({sig:.0f} dBm)"))
        elif sig is not None and sig <= t["signal_weak_dbm"]:
            issues.append(("warn", f"weak signal ({sig:.0f} dBm)"))
        tx = _num(c.get("tx_packets")) or 0
        retries = _num(c.get("tx_retries")) or 0
        if tx >= t["min_tx_packets"] and retries:
            pct = 100.0 * retries / tx
            if pct >= t["retry_pct_bad"]:
                issues.append(("bad", f"{pct:.0f}% Wi-Fi retries"))
            elif pct >= t["retry_pct_warn"]:
                issues.append(("warn", f"{pct:.0f}% Wi-Fi retries"))
    elif isinstance(port, dict):
        errors = (_num(port.get("rx_errors")) or 0) + (_num(port.get("tx_errors")) or 0)
        if errors:
            issues.append(("warn", f"{int(errors):,} errors on switch port"))
        if port.get("full_duplex") is False and port.get("up", True):
            issues.append(("warn", "half duplex"))
    sat = _num(c.get("satisfaction"))
    if sat is not None and sat < t["satisfaction"]:
        issues.append(("bad" if sat < t["satisfaction"] / 2 else "warn", f"satisfaction {sat:.0f}%"))
    if flaps >= t["flaps_24h"]:
        issues.append(("bad", f"flapping: {flaps} connect/disconnect events in 24 h"))
    return issues


def _dev_node(d):
    up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
    stats = d.get("system-stats") if isinstance(d.get("system-stats"), dict) else {}
    wan = d.get("wan1") if isinstance(d.get("wan1"), dict) else {}
    return {
        "mac": d["mac"].lower(), "name": d.get("name") or d.get("hostname") or d["mac"],
        "model": d.get("model") or "", "type": d.get("type") or "", "ip": d.get("ip") or "",
        "version": d.get("version") or "", "online": d.get("state", 0) == 1,
        "cpu": _num(stats.get("cpu")), "mem": _num(stats.get("mem")),
        "clients": _num(d.get("num_sta")) or 0,
        "uplink_port": _num(up.get("port_idx")), "uplink_remote_port": _num(up.get("uplink_remote_port")),
        "wan_ip": wan.get("ip") or (up.get("ip") if d.get("type") in GATEWAY_TYPES else None),
        "isp": wan.get("isp_name") or None,
    }


def build_model(devices, clients, events=None, network=None, site=None, t=THRESHOLDS):
    devs = {}
    for d in devices or []:
        if isinstance(d, dict) and d.get("mac"):
            devs[d["mac"].lower()] = d
    nodes = {m: _dev_node(d) for m, d in devs.items()}
    children, roots = {}, []
    for mac, d in devs.items():
        up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        parent = str(up.get("uplink_mac") or "").lower()
        if parent in devs and parent != mac:
            children.setdefault(parent, []).append(mac)
        else:
            roots.append(mac)
    order = lambda m: (nodes[m]["type"] not in GATEWAY_TYPES, nodes[m]["type"] != "usw", nodes[m]["name"].lower())  # noqa: E731
    roots.sort(key=order)
    for lst in children.values():
        lst.sort(key=order)

    flaps = count_flaps(events)
    ports = {m: {_num(p.get("port_idx")): p for p in (d.get("port_table") or []) if isinstance(p, dict)}
             for m, d in devs.items()}
    problems, n_clients = {}, 0
    for c in clients or []:
        if not isinstance(c, dict) or not c.get("mac"):
            continue
        n_clients += 1
        mac = c["mac"].lower()
        wired = bool(c.get("is_wired"))
        parent = str((c.get("sw_mac") if wired else c.get("ap_mac")) or c.get("uplink_mac") or "").lower()
        port = ports.get(parent, {}).get(_num(c.get("sw_port"))) if wired else None
        issues = client_issues(c, port, flaps.get(mac, 0), t)
        if not issues:
            continue
        if parent not in devs:
            parent = roots[0] if roots else ""
        entry = {
            "mac": mac, "name": c.get("name") or c.get("hostname") or c.get("oui") or mac,
            "ip": c.get("ip") or "", "wired": wired, "parent": parent,
            "severity": "bad" if any(s == "bad" for s, _ in issues) else "warn",
            "issues": [txt for _, txt in issues],
        }
        if wired:
            entry["port"] = _num(c.get("sw_port"))
        else:
            entry.update(ssid=c.get("essid") or "", channel=_num(c.get("channel")),
                         radio=c.get("radio_proto") or c.get("radio") or "", signal=_num(c.get("signal")))
        problems.setdefault(parent, []).append(entry)
    for lst in problems.values():
        lst.sort(key=lambda e: (e["severity"] != "bad", e["name"].lower()))
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "network": network, "site": site,
        "devices": nodes, "children": children, "roots": roots, "problems": problems,
        "totals": {"devices": len(nodes), "clients": n_clients,
                   "problem_clients": sum(len(v) for v in problems.values())},
        "thresholds": dict(t),
    }


# ── layout + SVG ──────────────────────────────────────────────────────────────
DEV_W, DEV_H = 230, 74
CL_W, CL_BASE, CL_LINE = 250, 58, 14
WAN_H, GAP_X, GAP_Y, MARGIN, HEADER = 40, 28, 80, 40, 64
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO = "ui-monospace, Consolas, monospace"
C = {"bg": "#0a0c10", "box": "#161922", "line": "#2a2f3d", "text": "#e2e8f0", "muted": "#8b95a7",
     "accent": "#00d4ff", "ok": "#00e5a0", "warn": "#ffb020", "bad": "#ff4d6d", "edge": "#3a4152"}


def _e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _fit(s, n):
    s = str(s if s is not None else "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _client_h(c):
    return CL_BASE + CL_LINE * len(c["issues"])


def layout(model):
    """Tidy top-down tree. Returns (nodes, width, height). Each node: kind d/c, x, y, w, h."""
    devs, children, problems = model["devices"], model["children"], model["problems"]
    clients = {c["mac"]: c for lst in problems.values() for c in lst}
    kids = lambda mac: [("d", m) for m in children.get(mac, [])] + [("c", c["mac"]) for c in problems.get(mac, [])]  # noqa: E731
    size = lambda k, key: (DEV_W, DEV_H) if k == "d" else (CL_W, _client_h(clients[key]))  # noqa: E731
    widths, heights = {}, {}

    def measure(k, key, depth):
        w, h = size(k, key)
        heights[depth] = max(heights.get(depth, 0), h)
        ks = kids(key) if k == "d" else []
        total = sum(measure(ck, cm, depth + 1) for ck, cm in ks) + GAP_X * max(0, len(ks) - 1)
        widths[(k, key)] = max(w, total)
        return widths[(k, key)]

    roots = model["roots"]
    total_w = sum(measure("d", r, 0) for r in roots) + GAP_X * max(0, len(roots) - 1)
    has_wan = any(devs[r].get("wan_ip") for r in roots)
    ys, y = {}, MARGIN + HEADER + (WAN_H + GAP_Y if has_wan else 0)
    for d in sorted(heights):
        ys[d] = y
        y += heights[d] + GAP_Y
    nodes = {}

    def place(k, key, left, depth, parent):
        w, h = size(k, key)
        sw = widths[(k, key)]
        nodes[f"{k}:{key}"] = {"kind": k, "key": key, "x": left + (sw - w) / 2, "y": ys[depth], "w": w, "h": h,
                               "parent": parent, "depth": depth}
        if k == "d":
            ks = kids(key)
            inner = sum(widths[(ck, cm)] for ck, cm in ks) + GAP_X * max(0, len(ks) - 1)
            cx = left + (sw - inner) / 2
            for ck, cm in ks:
                place(ck, cm, cx, depth + 1, f"d:{key}")
                cx += widths[(ck, cm)] + GAP_X

    left = MARGIN
    for r in roots:
        place("d", r, left, 0, None)
        left += widths[("d", r)] + GAP_X
    width = max(total_w, 640) + 2 * MARGIN
    height = (y - GAP_Y if heights else MARGIN + HEADER) + MARGIN + 36
    return nodes, width, height


def _bezier(x0, y0, x1, y1, t):
    ym = (y0 + y1) / 2
    px = (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * x0 + 3 * (1 - t) * t ** 2 * x1 + t ** 3 * x1
    py = (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * ym + 3 * (1 - t) * t ** 2 * ym + t ** 3 * y1
    return px, py


def _badge(x, y, text, color):
    tw = 7 * len(text) + 10
    return (f'<rect x="{x - tw / 2:.1f}" y="{y - 9}" width="{tw}" height="18" rx="4" fill="{C["box"]}" stroke="{color}"/>'
            f'<text x="{x:.1f}" y="{y + 4}" text-anchor="middle" font-size="11" font-family="{MONO}" fill="{color}">{_e(text)}</text>')


def _text(x, y, s, size=12, fill=C["text"], weight="normal", anchor="start", mono=False):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="{MONO if mono else FONT}" fill="{fill}">{_e(s)}</text>')


def render_svg(model):
    nodes, W, H = layout(model)
    devs, problems = model["devices"], model["problems"]
    clients = {c["mac"]: c for lst in problems.values() for c in lst}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" '
           f'font-family="{FONT}">',
           f'<rect width="100%" height="100%" fill="{C["bg"]}"/>']
    tot = model["totals"]
    title = f"Network topology — {model.get('network') or 'UniFi'}"
    when = model["generated_at"].replace("T", " ")[:16]
    out.append(_text(MARGIN, MARGIN + 4, title, 20, weight="bold"))
    out.append(_text(MARGIN, MARGIN + 26, f"{when} · site {model.get('site') or 'default'} · {tot['devices']} UniFi devices · "
                     f"{tot['clients']} clients connected · {tot['problem_clients']} with issues (only those are drawn)",
                     12, C["muted"]))

    # edges first, so boxes sit on top
    for nid, n in nodes.items():
        if not n["parent"]:
            continue
        p = nodes[n["parent"]]
        x0, y0 = p["x"] + p["w"] / 2, p["y"] + p["h"]
        x1, y1 = n["x"] + n["w"] / 2, n["y"]
        ym = (y0 + y1) / 2
        if n["kind"] == "d":
            d = devs[n["key"]]
            color = C["edge"] if d["online"] else C["bad"]
            out.append(f'<path d="M{x0:.1f},{y0:.1f} C{x0:.1f},{ym:.1f} {x1:.1f},{ym:.1f} {x1:.1f},{y1:.1f}" '
                       f'fill="none" stroke="{color}" stroke-width="2"/>')
            if d.get("uplink_remote_port") is not None:
                bx, by = _bezier(x0, y0, x1, y1, 0.18)
                out.append(_badge(bx, by, str(int(d["uplink_remote_port"])), C["muted"]))
            if d.get("uplink_port") is not None:
                bx, by = _bezier(x0, y0, x1, y1, 0.84)
                out.append(_badge(bx, by, str(int(d["uplink_port"])), C["accent"]))
        else:
            c = clients[n["key"]]
            color = C[c["severity"]]
            dash = '' if c["wired"] else ' stroke-dasharray="6 4"'
            out.append(f'<path d="M{x0:.1f},{y0:.1f} C{x0:.1f},{ym:.1f} {x1:.1f},{ym:.1f} {x1:.1f},{y1:.1f}" '
                       f'fill="none" stroke="{color}" stroke-width="1.5"{dash}/>')
            # At the client end: siblings share the parent end, so badges there would stack.
            bx, by = _bezier(x0, y0, x1, y1, 0.82)
            out.append(_badge(bx, by, f"port {c['port']}" if c["wired"] and c.get("port") is not None
                              else ("wired" if c["wired"] else "Wi-Fi"), color))

    # WAN boxes above gateways
    for r in model["roots"]:
        d = devs[r]
        if not d.get("wan_ip"):
            continue
        n = nodes[f"d:{r}"]
        x, y = n["x"], n["y"] - GAP_Y - WAN_H
        out.append(f'<line x1="{x + n["w"] / 2:.1f}" y1="{y + WAN_H}" x2="{x + n["w"] / 2:.1f}" y2="{n["y"]}" '
                   f'stroke="{C["edge"]}" stroke-width="2"/>')
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{n["w"]}" height="{WAN_H}" rx="8" fill="{C["box"]}" stroke="{C["line"]}"/>')
        out.append(_text(x + 12, y + 17, d.get("isp") or "Internet", 12, weight="bold"))
        out.append(_text(x + 12, y + 32, f"WAN {d['wan_ip']}", 11, C["muted"], mono=True))

    for nid, n in nodes.items():
        x, y, w, h = n["x"], n["y"], n["w"], n["h"]
        if n["kind"] == "d":
            d = devs[n["key"]]
            stroke = C["line"] if d["online"] else C["bad"]
            tip = (f"{d['name']} · {d['model']} · {d['ip']} · {d['mac']} · firmware {d['version']} · "
                   f"{d['clients']} clients · {'online' if d['online'] else 'OFFLINE'}")
            out.append(f'<g><title>{_e(tip)}</title>')
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" rx="10" fill="{C["box"]}" stroke="{stroke}" stroke-width="1.5"/>')
            out.append(f'<circle cx="{x + 14:.1f}" cy="{y + 18:.1f}" r="4" fill="{C["ok"] if d["online"] else C["bad"]}"/>')
            out.append(_text(x + 24, y + 22, _fit(d["name"], 22), 13, weight="bold"))
            stats = " · ".join(s for s in [f"CPU {d['cpu']:.0f}%" if d["cpu"] is not None else "",
                                           f"RAM {d['mem']:.0f}%" if d["mem"] is not None else ""] if s)
            if stats:
                out.append(_text(x + w - 10, y + 22, stats, 10, C["muted"], anchor="end", mono=True))
            out.append(_text(x + 24, y + 40, _fit(f"{d['model']} · {d['ip']}", 34), 11, C["muted"]))
            line3 = f"v{d['version']} · {d['clients']} clients" if d["version"] else f"{d['clients']} clients"
            if not d["online"]:
                line3 = "OFFLINE · " + line3
            n_prob = len(problems.get(n["key"], []))
            if n_prob:
                line3 += f" · {n_prob} with issues"
            out.append(_text(x + 24, y + 57, _fit(line3, 36), 11, C["bad"] if not d["online"] else C["muted"], mono=True))
            out.append('</g>')
        else:
            c = clients[n["key"]]
            color = C[c["severity"]]
            tip = f"{c['name']} · {c['ip']} · {c['mac']} · " + "; ".join(c["issues"])
            out.append(f'<g><title>{_e(tip)}</title>')
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" rx="8" fill="{C["box"]}" stroke="{color}" stroke-width="1.5"/>')
            out.append(_text(x + 12, y + 19, _fit(c["name"], 30), 12, weight="bold"))
            out.append(_text(x + 12, y + 35, f"{c['ip'] or 'no IP'} · {c['mac']}", 10, C["muted"], mono=True))
            if c["wired"]:
                conn = f"wired · port {c['port']}" if c.get("port") is not None else "wired"
            else:
                parts = [c.get("ssid") or "Wi-Fi"]
                if c.get("channel") is not None:
                    parts.append(f"ch {int(c['channel'])}")
                if c.get("radio"):
                    parts.append(str(c["radio"]))
                if c.get("signal") is not None:
                    parts.append(f"{c['signal']:.0f} dBm")
                conn = " · ".join(parts)
            out.append(_text(x + 12, y + 50, _fit(conn, 38), 10, C["muted"]))
            for i, issue in enumerate(c["issues"]):
                out.append(_text(x + 12, y + 50 + CL_LINE * (i + 1), _fit("⚠ " + issue, 38), 11, color))
            out.append('</g>')

    # legend
    ly = H - 22
    out.append(f'<line x1="{MARGIN}" y1="{ly}" x2="{MARGIN + 24}" y2="{ly}" stroke="{C["edge"]}" stroke-width="2"/>')
    out.append(_text(MARGIN + 30, ly + 4, "UniFi uplink (port numbers at each end)", 11, C["muted"]))
    out.append(f'<line x1="{MARGIN + 290}" y1="{ly}" x2="{MARGIN + 314}" y2="{ly}" stroke="{C["warn"]}" stroke-width="1.5"/>')
    out.append(_text(MARGIN + 320, ly + 4, "client with a warning", 11, C["muted"]))
    out.append(f'<line x1="{MARGIN + 470}" y1="{ly}" x2="{MARGIN + 494}" y2="{ly}" stroke="{C["bad"]}" stroke-width="1.5"/>')
    out.append(_text(MARGIN + 500, ly + 4, "client with a serious issue (dashed = Wi-Fi)", 11, C["muted"]))
    if not tot["problem_clients"]:
        out.append(_text(W - MARGIN, ly + 4, "No clients with issues right now.", 11, C["ok"], anchor="end"))
    out.append("</svg>")
    return "\n".join(out)
