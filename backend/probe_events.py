"""
Diagnostic: check which UniFi endpoints the ACTIVE connection answers.

Run from the project folder:
    .venv\\Scripts\\python backend\\probe_events.py        (Windows)
    .venv/bin/python backend/probe_events.py              (macOS/Linux)

Uses the connection currently selected in the app (no credentials in this file).
Paste the output into a chat when asking for help - it contains no secrets.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_store as cfg                   # noqa: E402
from unifi_client import UniFiClient, UniFiError, format_fingerprint  # noqa: E402


def main():
    p = cfg.active_profile()
    if not p:
        print("No active connection. Add one in the app first.")
        return
    c = UniFiClient(p, cfg.get_secret(p["id"]))
    print("=" * 64)
    print(f"connection : {p['name']}  {c.base_url}  site={c.site}")
    print(f"console    : {c.console_type}   auth={c.auth}")
    try:
        c.login()
        print(f"login      : ok   csrf={'yes' if c.csrf_token else 'no'}")
    except UniFiError as e:
        print(f"login      : FAILED - {e}")
        return
    print(f"certificate: {format_fingerprint(c.last_fingerprint)}")
    print("=" * 64)

    def probe(label, fn):
        try:
            r = fn()
            n = len(r) if isinstance(r, (list, dict)) else "?"
            print(f"OK    {label:<34} -> {n} items")
        except UniFiError as e:
            print(f"FAIL  {label:<34} -> {e}")

    probe("sites", c.list_sites)
    probe("stat/health", c.get_health)
    probe("stat/device", c.get_devices)
    probe("stat/sta (clients)", c.get_clients)
    probe("alarms", c.get_alerts)
    probe("v2 system-log/all", lambda: c.get_events(limit=10))
    probe("v2 system-log (INTERNET_AND_WAN)", lambda: c.get_events(limit=10, categories=["INTERNET_AND_WAN"]))
    probe("rest/wlanconf", c.get_wlans)
    probe("rest/networkconf", c.get_networks)
    probe("rest/firewallrule", c.get_firewall_rules)
    probe("rest/portforward", c.get_port_forwards)
    probe("stat/sysinfo", c.get_system_info)
    print("=" * 64)


if __name__ == "__main__":
    main()
