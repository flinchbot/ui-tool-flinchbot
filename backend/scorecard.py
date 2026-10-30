"""
Security scorecard: rule-based checks over the console's own configuration (no AI,
no guessing), plus an optional Nmap sweep for risky open ports on LAN devices.

Every finding says what was found, why it matters, how to fix it in the UniFi UI,
and carries a prompt for "Fix with flinchbot" (the AI then proposes the change
through the usual Approve card, with Undo where the app supports it).

grade(findings, unchecked) turns them into a 0-100 score and a letter, or no grade
("?") when a check couldn't run because its data couldn't be read: unknown is not safe.
coverage(data) lists those checks.
"""

import re
from datetime import datetime

WEIGHT = {"critical": 25, "high": 12, "medium": 6, "low": 2}
ORDER = ["critical", "high", "medium", "low"]
IOT_VENDORS = re.compile(r"espressif|tuya|shelly|roborock|govee|wyze|hikvision|dahua|reolink|ring|amazon|"
                         r"google|nest|sonos|ecobee|tp-?link|kasa|meross|lifx|broadlink|xiaomi|switchbot|"
                         r"philips|signify|ezviz|eufy|arlo|blink|chamberlain|myq|rachio|wiz", re.I)
RISKY_FWD = {22: "SSH", 23: "Telnet", 21: "FTP", 445: "SMB", 139: "NetBIOS", 3389: "Remote Desktop", 5900: "VNC",
             3306: "MySQL", 5432: "PostgreSQL", 1433: "SQL Server", 6379: "Redis", 27017: "MongoDB", 8291: "Winbox",
             161: "SNMP", 1900: "UPnP"}


def _f(severity, category, title, detail, fix, ask, affected=None, fid=None):
    return {"id": fid or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60], "severity": severity,
            "category": category, "title": title, "detail": detail, "fix": fix, "ask": ask,
            "affected": affected or []}


def _settings(settings):
    return {s.get("key"): s for s in settings or [] if isinstance(s, dict)}


def _ports(spec):
    """'80,443,8000-8010' -> set of ints (ranges capped)."""
    out = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = int(a), int(b)
                if hi - lo > 2000:
                    return {"*"}
                out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def check(data):
    """data: dict of wlans, networks, devices, clients, settings, port_forwards,
    policies, zones, fw_rules (each a list; missing = not checked).
    -> (findings, passed)"""
    F, passed = [], []
    S = _settings(data.get("settings"))
    wlans = [w for w in data.get("wlans") or [] if isinstance(w, dict)]
    nets = [n for n in data.get("networks") or [] if isinstance(n, dict)]

    # ── Wi-Fi ────────────────────────────────────────────────────────────────
    for w in wlans:
        if w.get("enabled") is False:
            continue
        name, sec = w.get("name") or "?", (w.get("security") or "").lower()
        ask_fix = f"Look at the Wi-Fi network “{name}” and"
        if sec == "open" and not w.get("is_guest"):
            F.append(_f("critical", "Wi-Fi", f"“{name}” has no password",
                        "Anyone in range can join and reach your devices.",
                        ["Settings → WiFi → " + name + " → Security: WPA2/WPA3 Personal, set a long password."],
                        f"{ask_fix} propose securing it with WPA2/WPA3 (I'll set the password myself).", [name], f"open-{name}"))
        elif sec == "open":
            F.append(_f("medium", "Wi-Fi", f"Guest Wi-Fi “{name}” is open",
                        "Open guest networks are common, but traffic on them is unencrypted and anyone can use your internet.",
                        ["Use a password (WPA2/WPA3) or a hotspot portal with vouchers."],
                        f"{ask_fix} tell me the safest way to keep guest access easy.", [name], f"openguest-{name}"))
        if sec.startswith("wpa"):
            mode, enc = (w.get("wpa_mode") or "").lower(), (w.get("wpa_enc") or "").lower()
            if mode in ("wpa1", "wpa") or "tkip" in enc:
                F.append(_f("high", "Wi-Fi", f"“{name}” allows old WPA/TKIP",
                            "WPA1 and TKIP are broken and also slow every device on that network down.",
                            [f"Settings → WiFi → {name} → Security: WPA2 (AES) or WPA2/WPA3."],
                            f"{ask_fix} propose switching it to WPA2/WPA3 with AES only.", [name], f"tkip-{name}"))
            elif not (w.get("wpa3_support") or w.get("wpa3_transition") or mode == "wpa3"):
                F.append(_f("low", "Wi-Fi", f"“{name}” is WPA2 only",
                            "WPA3 (or WPA2/WPA3 transition) protects the password against offline guessing. "
                            "Very old devices may need WPA2, which transition mode keeps working.",
                            [f"Settings → WiFi → {name} → Security → WPA2/WPA3."],
                            f"{ask_fix} propose turning on WPA2/WPA3 transition mode if all my devices support it.", [name], f"wpa2-{name}"))
            if (w.get("pmf_mode") or "").lower() == "disabled" and (w.get("wpa3_support") or mode == "wpa3"):
                F.append(_f("low", "Wi-Fi", f"“{name}”: Protected Management Frames off",
                            "PMF stops deauthentication attacks and is required for WPA3.",
                            [f"Settings → WiFi → {name} → Advanced → PMF: Optional or Required."],
                            f"{ask_fix} propose setting PMF to optional.", [name], f"pmf-{name}"))
            pw = w.get("x_passphrase")
            if isinstance(pw, str) and 0 < len(pw) < 12:
                F.append(_f("medium", "Wi-Fi", f"“{name}” has a short password ({len(pw)} characters)",
                            "Short Wi-Fi passwords can be guessed offline from one captured login. 12+ characters, "
                            "or a few random words, is much safer.",
                            [f"Settings → WiFi → {name} → Password: 12+ characters (all devices must reconnect)."],
                            f"{ask_fix} tell me what changing its password involves (don't change it yet).", [name], f"shortpw-{name}"))
        if w.get("is_guest") and not w.get("l2_isolation") and sec != "open":
            pass   # guest isolation is usually enforced by the guest network itself
    if wlans and not any(f["category"] == "Wi-Fi" for f in F):
        passed.append("Wi-Fi networks use WPA2/WPA3 with sound settings")

    # ── firmware ─────────────────────────────────────────────────────────────
    devs = [d for d in data.get("devices") or [] if isinstance(d, dict)]
    old = [d for d in devs if d.get("upgradable")]
    if old:
        F.append(_f("medium" if len(old) < 3 else "high", "Firmware",
                    f"{len(old)} UniFi device{'s' if len(old) > 1 else ''} with a firmware update waiting",
                    "Firmware updates fix security holes as well as bugs.",
                    ["UniFi Devices → select the device → Update (or Settings → System → Updates to schedule them)."],
                    "Which of my UniFi devices need firmware updates, what's in them, and in what order should I update them?",
                    [f"{d.get('name') or d.get('mac')} {d.get('version')} → {d.get('upgrade_to_firmware') or 'newer'}" for d in old],
                    "firmware"))
    elif devs:
        passed.append("All UniFi devices are on current firmware")

    # ── gateway services ─────────────────────────────────────────────────────
    usg = S.get("usg") or {}
    if usg.get("upnp_enabled"):
        F.append(_f("high", "Gateway", "UPnP is on",
                    "UPnP lets any device on the LAN open ports to the internet without asking, which malware uses "
                    "to expose itself. Turn it off unless a game console needs it, and then only for that network.",
                    ["Settings → Internet → UPnP: off (or limit it to one network)."],
                    "UPnP is on. Explain the risk for my network and propose turning it off or limiting it.",
                    ["UPnP" + (" + NAT-PMP" if usg.get("upnp_nat_pmp_enabled") else "")], "upnp"))
    elif "usg" in S:
        passed.append("UPnP is off")
    ips = S.get("ips") or {}
    if "ips" in S and (ips.get("ips_mode") in (None, "", "disabled")):
        F.append(_f("medium", "Gateway", "Intrusion prevention (IPS/IDS) is off",
                    "UniFi's IPS blocks known attacks and connections to known-bad hosts. It costs some throughput.",
                    ["Settings → CyberSecure (or Security) → Intrusion Prevention: Notify and Block."],
                    "Intrusion prevention is off. What would turning it on cost me in speed on this gateway, and what does it protect?",
                    [], "ips"))
    elif "ips" in S:
        passed.append(f"Intrusion prevention is on ({ips.get('ips_mode')})")
    mgmt = S.get("mgmt") or {}
    if mgmt.get("x_ssh_enabled") and mgmt.get("x_ssh_auth_password_enabled", True) and not mgmt.get("x_ssh_keys"):
        F.append(_f("low", "Gateway", "Device SSH allows password logins",
                    "SSH to your UniFi devices is on with a password. Fine on a trusted LAN; use a long password, "
                    "or SSH keys, and turn it off when you don't need it.",
                    ["Settings → System → Advanced → Device SSH Authentication."],
                    "Device SSH is on with password login. Is that a risk on my network, and what should I do?", [], "ssh"))
    sm = S.get("super_mgmt") or {}
    if "super_mgmt" in S and sm.get("autobackup_enabled") is False:
        F.append(_f("low", "Resilience", "Automatic backups are off",
                    "Without them, a dead console means rebuilding the network by hand.",
                    ["Settings → System → Backups → Auto Backup: on."],
                    "Automatic config backups are off. How do I turn them on and where do they go?", [], "autobackup"))

    # ── port forwards ────────────────────────────────────────────────────────
    for pf in data.get("port_forwards") or []:
        if not isinstance(pf, dict) or pf.get("enabled") is False:
            continue
        ports = _ports(pf.get("dst_port"))
        name = pf.get("name") or f"port {pf.get('dst_port')}"
        target = f"{pf.get('fwd')}:{pf.get('fwd_port') or pf.get('dst_port')}"
        open_to_all = (pf.get("src") or "any") in ("any", "", None) and not pf.get("src_firewall_group_id")
        if "*" in ports or len(ports) > 200:
            F.append(_f("critical", "Port forwards", f"Port forward “{name}” opens a huge range",
                        f"WAN ports {pf.get('dst_port')} go to {target}: that device is fully exposed to the internet.",
                        ["Settings → Routing → Port forwarding: forward only the ports you need."],
                        f"My port forward “{name}” opens ports {pf.get('dst_port')}. Help me narrow it down.", [name]))
            continue
        risky = sorted(p for p in ports if p in RISKY_FWD)
        if risky:
            F.append(_f("high" if open_to_all else "medium", "Port forwards",
                        f"{', '.join(RISKY_FWD[p] for p in risky)} is reachable from the internet",
                        f"“{name}” forwards WAN port {', '.join(map(str, risky))} to {target}"
                        + (" from any address." if open_to_all else " (source-limited).")
                        + " These services are scanned and attacked constantly. A VPN (WireGuard/Teleport) is safer.",
                        ["Remove the forward and use UniFi's VPN server or Teleport, or limit the source addresses."],
                        f"My port forward “{name}” exposes {', '.join(RISKY_FWD[p] for p in risky)}. Propose a safer setup.", [name]))
        elif open_to_all:
            F.append(_f("low", "Port forwards", f"Port forward “{name}”",
                        f"WAN port {pf.get('dst_port')} goes to {target} from anywhere. Make sure that device is patched.",
                        ["Limit the source if you can (Settings → Routing → Port forwarding → Source)."],
                        f"Review my port forward “{name}”: is it still needed and is it safe?", [name]))
    if data.get("port_forwards") == [] and "port_forwards" not in (data.get("unavailable") or {}):
        passed.append("No port forwards: nothing on the LAN is exposed directly")

    # ── firewall (zone-based) ────────────────────────────────────────────────
    zones = {z.get("_id"): z for z in data.get("zones") or [] if isinstance(z, dict)}
    zname = lambda zid: (zones.get(zid) or {}).get("name") or "?"
    zkey = lambda zid: (zones.get(zid) or {}).get("zone_key")
    for pol in data.get("policies") or []:
        if not isinstance(pol, dict) or pol.get("predefined") or pol.get("enabled") is False:
            continue
        if (pol.get("action") or "").upper() != "ALLOW":
            continue
        s, d = pol.get("source") or {}, pol.get("destination") or {}
        broad = s.get("matching_target") in (None, "ANY") and d.get("matching_target") in (None, "ANY") \
            and (pol.get("protocol") in (None, "all")) and d.get("port_matching_type") in (None, "ANY") \
            and s.get("port_matching_type") in (None, "ANY")
        name = pol.get("name") or "?"
        if s.get("port_matching_type") == "SPECIFIC" and s.get("port") and d.get("port_matching_type") in (None, "ANY"):
            F.append(_f("low", "Firewall", f"“{name}” matches a SOURCE port ({s.get('port')})",
                        f"It allows traffic whose source port is {s.get('port')}, to any destination port. Clients pick "
                        f"random source ports, so this almost never matches; the port probably belongs on the destination.",
                        ["Settings → Policy Engine → Firewall → edit the policy: move the port to Destination."],
                        f"My firewall policy “{name}” has port {s.get('port')} on the source side. Check whether it should be "
                        f"the destination port and propose the fix.", [name], f"srcport-{name}"))
            continue
        if zkey(s.get("zone_id")) == "external" and zkey(d.get("zone_id")) in ("internal", None):
            F.append(_f("critical" if broad else "high", "Firewall", f"Policy “{name}” lets the internet in",
                        f"It allows traffic from {zname(s.get('zone_id'))} to {zname(d.get('zone_id'))}"
                        + (" with no port or address limits." if broad else "."),
                        ["Settings → Policy Engine → Zones / Firewall: remove it or narrow it to one host and port."],
                        f"Review my firewall policy “{name}”: it allows internet traffic into my LAN. Propose a safer version.", [name]))
        elif broad and zkey(s.get("zone_id")) != zkey(d.get("zone_id")) and zkey(d.get("zone_id")) == "internal":
            F.append(_f("medium", "Firewall", f"“{name}” gives {zname(s.get('zone_id'))} full access to {zname(d.get('zone_id'))}",
                        f"Any device in {zname(s.get('zone_id'))} can reach every device and port in {zname(d.get('zone_id'))}, "
                        "which undoes the point of separating them. Allow only what's needed (e.g. one host and port).",
                        ["Settings → Policy Engine → Firewall: replace it with specific allow rules."],
                        f"My firewall policy “{name}” allows {zname(s.get('zone_id'))} to reach all of {zname(d.get('zone_id'))}. "
                        f"Work out which devices actually need that and propose narrower policies.", [name]))

    # ── segmentation ─────────────────────────────────────────────────────────
    lans = [n for n in nets if n.get("purpose") in ("corporate", "guest") and n.get("enabled", True)]
    main = next((n for n in lans if n.get("attr_hidden_id") == "LAN"), None)
    clients = [c for c in data.get("clients") or [] if isinstance(c, dict)]
    if len(lans) <= 1 and clients:
        iot = [c for c in clients if IOT_VENDORS.search(str(c.get("oui") or ""))]
        if iot:
            F.append(_f("medium", "Segmentation", "Smart-home devices share the LAN with your computers",
                        f"{len(iot)} smart-home/IoT devices are on the same network as everything else. If one is "
                        "hacked, it can reach your PCs and NAS. A separate IoT VLAN with a firewall zone fixes that.",
                        ["Create an IoT network (VLAN) and Wi-Fi, move smart devices to it, allow only what's needed."],
                        "Plan an IoT VLAN for my network: which devices should move, and what firewall rules they need.",
                        [f"{c.get('name') or c.get('hostname') or c.get('mac')} ({c.get('oui')})" for c in iot[:15]], "segmentation"))
    elif main and clients:
        iot_main = [c for c in clients if IOT_VENDORS.search(str(c.get("oui") or "")) and
                    (c.get("network") == main.get("name") or c.get("network_id") == main.get("_id"))]
        if len(iot_main) >= 3:
            F.append(_f("low", "Segmentation", f"{len(iot_main)} smart-home devices are still on the main LAN",
                        f"You have separate networks, but these look like IoT devices and sit on “{main.get('name')}”.",
                        ["Move them to the IoT Wi-Fi/VLAN (or set a virtual network override per client)."],
                        "Which smart-home devices are still on my main LAN, and how do I move them to the IoT network?",
                        [f"{c.get('name') or c.get('hostname') or c.get('mac')} ({c.get('oui')})" for c in iot_main[:15]], "iot-on-lan"))
        else:
            passed.append("Smart-home devices are separated from the main LAN")
    return F, passed


def from_scan(hosts, clients):
    """Turn an Nmap sweep (nmap_scan.sweep) into findings."""
    by_ip = {c.get("ip"): c for c in clients or [] if isinstance(c, dict) and c.get("ip")}
    F = []
    severe = {23, 2323, 21, 445, 6379, 27017, 5900}
    for h in hosts:
        c = by_ip.get(h["ip"], {})
        who = c.get("name") or c.get("hostname") or h["ip"]
        ports = h["open_ports"]
        top = "high" if any(p["port"] in severe for p in ports) else "medium" if any(p["port"] in (3389, 1883, 554, 139) for p in ports) else "low"
        F.append(_f(top, "Open ports", f"{who} has {len(ports)} risky port{'s' if len(ports) > 1 else ''} open",
                    "; ".join(f"{p['port']}/{p['protocol']}: {p['risk']}" for p in ports),
                    ["Turn the service off in the device's settings, update its firmware, or move it to an isolated network."],
                    f"An Nmap scan found these open ports on {who} ({h['ip']}): "
                    + ", ".join(f"{p['port']} ({p.get('name') or '?'})" for p in ports)
                    + ". What is this device likely to be, how risky is that, and what should I do?",
                    [f"{who} ({h['ip']}, {c.get('mac') or h.get('mac') or ''})"], f"scan-{h['ip']}"))
    return F


AREAS = {"settings": "Gateway settings (UPnP, intrusion prevention, device SSH, automatic backups)",
         "port_forwards": "Port forwards", "policies": "Firewall policies", "zones": "Firewall zones",
         "networks": "Networks (IoT separation)"}


def coverage(data):
    """Checks that couldn't run. -> [{"area", "reason", "unsupported"}]. "unsupported": the console
    has no such feature (an older firmware), which doesn't stop a grade; anything else does."""
    out = []
    for key, err in (data.get("unavailable") or {}).items():
        unsupported = err.get("kind") == "notfound"
        out.append({"key": key, "area": AREAS.get(key, key), "unsupported": unsupported,
                    "reason": "not on this console" if unsupported else f"couldn't be read: {err.get('error')}"})
    return out


def grade(findings, unchecked=()):
    score = max(0, 100 - sum(WEIGHT.get(f["severity"], 0) for f in findings))
    letter = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    incomplete = any(not u["unsupported"] for u in unchecked or ())
    if incomplete:                # some checks never ran: a grade would claim more than we know
        score, letter = None, "?"
    return {"score": score, "grade": letter, "incomplete": incomplete,
            "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in ORDER},
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def sort(findings):
    return sorted(findings, key=lambda f: (ORDER.index(f["severity"]), f["category"], f["title"]))
