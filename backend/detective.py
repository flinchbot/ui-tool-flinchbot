"""
Device detective: which clients are unnamed or new, and what they probably are.

Clues used (no network traffic of our own unless the user runs an Nmap scan):
  * the console's own fingerprint guess (v2 clients/active: model_name, display_name)
  * the MAC vendor (OUI), and whether the MAC is a private/random one
    (phones, tablets and laptops make one up per Wi-Fi network)
  * the hostname the device announced, matched against common patterns
  * where it is connected (SSID / switch port), when it was first seen
"""

import re
import time

NEW_DAYS = 7

# hostname pattern -> what it usually is
_HOST_HINTS = [
    (r"iphone", "Apple iPhone"), (r"ipad", "Apple iPad"), (r"macbook|imac|mac-?mini|mac-?studio", "Apple Mac"),
    (r"apple-?tv", "Apple TV"), (r"homepod", "Apple HomePod"), (r"watch", "smartwatch"),
    (r"galaxy|sm-[a-z]\d", "Samsung Galaxy phone/tablet"), (r"pixel", "Google Pixel phone"),
    (r"android", "Android device"), (r"desktop-|laptop-|win-|pc-", "Windows PC"),
    (r"chromecast|google-?home|nest", "Google/Nest device"), (r"echo|amazon|fire-?tv|kindle", "Amazon device"),
    (r"roku", "Roku streamer"), (r"sonos", "Sonos speaker"), (r"bravia|lg-?tv|webos|samsung-?tv|tizen|vizio", "smart TV"),
    (r"esp[_-]?\d|esp32|esp8266|espressif", "ESP8266/ESP32 microcontroller (DIY or smart-home gadget)"),
    (r"tasmota", "Tasmota smart plug/switch"), (r"shelly", "Shelly smart relay"), (r"tuya|smartlife", "Tuya smart device"),
    (r"hue|philips", "Philips Hue"), (r"wled", "WLED light controller"), (r"ring", "Ring doorbell/camera"),
    (r"wyze", "Wyze camera"), (r"reolink|hikvision|dahua|amcrest|cam", "IP camera"),
    (r"printer|hp[a-f0-9]{6}|epson|brother|canon", "printer"), (r"nas|synology|diskstation|qnap|truenas", "NAS"),
    (r"raspberry|raspberrypi|rpi", "Raspberry Pi"), (r"homeassistant|hass", "Home Assistant"),
    (r"playstation|ps[45]", "PlayStation"), (r"xbox", "Xbox"), (r"switch|nintendo", "Nintendo Switch"),
    (r"roborock|roomba|irobot|vacuum|dreame|ecovacs", "robot vacuum"), (r"tesla", "Tesla car"),
]


def random_mac(mac):
    """Locally administered bit set: a made-up 'private' MAC (iOS, Android, Windows, macOS do this)."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError, AttributeError):
        return False


def hint_from_hostname(hostname):
    h = (hostname or "").lower()
    for pat, what in _HOST_HINTS:
        if re.search(pat, h):
            return what
    return None


def candidates(clients, v2_clients=None, known_users=None, now=None):
    """-> list of clients worth identifying, most interesting first."""
    now = now or time.time()
    v2 = {(c.get("mac") or "").lower(): c for c in v2_clients or [] if isinstance(c, dict)}
    users = {(u.get("mac") or "").lower(): u for u in known_users or [] if isinstance(u, dict)}
    out = []
    for c in clients or []:
        if not isinstance(c, dict) or not c.get("mac"):
            continue
        mac = c["mac"].lower()
        x, u = v2.get(mac, {}), users.get(mac, {})
        named = bool(c.get("name") or u.get("name"))
        first = c.get("first_seen") or u.get("first_seen") or x.get("first_seen")
        new = bool(first and now - float(first) < NEW_DAYS * 86400)
        if named and not new:
            continue
        guess = x.get("model_name") or None
        rnd = random_mac(mac)
        hh = hint_from_hostname(c.get("hostname"))
        clues = []
        if guess:
            clues.append(f"UniFi fingerprint: {guess}" + (f" ({x.get('fingerprint', {}).get('confidence')}% sure)"
                                                          if isinstance(x.get("fingerprint"), dict) and x["fingerprint"].get("confidence") else ""))
        if c.get("oui"):
            clues.append(f"Maker (from the MAC): {c['oui']}")
        if rnd:
            clues.append("Private (random) MAC: almost always a phone, tablet, laptop or watch hiding its real address")
        if hh:
            clues.append(f"Hostname “{c.get('hostname')}” looks like: {hh}")
        if not clues:
            clues.append("No fingerprint, maker or telling hostname")
        sw = x.get("last_uplink_name") or ""
        if c.get("essid"):
            where = f"Wi-Fi {c['essid']}"
        elif c.get("is_wired"):
            where = " ".join(b for b in ("wired", f"to {sw}" if sw else "",
                                         f"port {c['sw_port']}" if c.get("sw_port") is not None else "") if b)
        else:
            where = ""
        score = (0 if named else 4) + (3 if new else 0) + (2 if not guess else 0) + (1 if not c.get("hostname") else 0)
        out.append({
            "mac": c["mac"], "ip": c.get("ip") or "", "name": c.get("name") or u.get("name") or "",
            "hostname": c.get("hostname") or "", "vendor": c.get("oui") or "", "unifi_guess": guess,
            "display_name": x.get("display_name") or "", "random_mac": rnd, "hostname_hint": hh,
            "first_seen": int(first) if first else None, "new": new, "named": named,
            "wired": bool(c.get("is_wired")), "where": where, "network": c.get("network") or x.get("network_name") or "",
            "uplink": x.get("last_uplink_name") or "", "clues": clues, "score": score,
        })
    out.sort(key=lambda d: (-d["score"], d["hostname"] or d["mac"]))
    return out
