"""Two fake consoles for the factory-reset kit test: one configured, one factory-fresh
(same built-ins, different ids), plus a helper that runs the generated script."""

import os
import shutil
import subprocess
import tempfile

WG_CONF = "[Interface]\nPrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSByZWFsIGtleQ==\nAddress = 10.75.0.2/32\n\n[Peer]\nPublicKey = cHVibGlj\nEndpoint = 1.2.3.4:51820\n"
WIFI_PW = 'pa"ss\\wo$rd\'x `y`'          # quotes, backslash, $ and backticks: must survive shell + JSON


def _id(n, seed):
    return f"{seed:02x}{n:022x}"


def configured(seed=0xa1):
    i = lambda n: _id(n, seed)
    lan, wan, iot, vpn = i(1), i(2), i(3), i(4)
    z_int, z_ext, z_iot = i(10), i(11), i(12)
    apg, ug = i(20), i(21)
    return {
        "networkconf": {
            lan: {"_id": lan, "name": "Default", "purpose": "corporate", "attr_hidden_id": "LAN", "attr_no_delete": True,
                  "ip_subnet": "10.42.0.1/16", "dhcpd_enabled": True, "dhcpd_start": "10.42.0.50", "dhcpd_stop": "10.42.3.250",
                  "firewall_zone_id": z_int},
            wan: {"_id": wan, "name": "Internet 1", "purpose": "wan", "attr_hidden_id": "WAN", "attr_no_delete": True,
                  "wan_networkgroup": "WAN", "wan_type": "dhcp", "firewall_zone_id": z_ext},
            iot: {"_id": iot, "name": "IoT", "purpose": "corporate", "vlan": 2, "vlan_enabled": True,
                  "ip_subnet": "192.168.2.1/24", "dhcpd_enabled": True, "dhcpd_start": "192.168.2.6",
                  "dhcpd_stop": "192.168.2.254", "firewall_zone_id": z_iot},
            vpn: {"_id": vpn, "name": "Mullvad CA", "purpose": "vpn-client", "vpn_type": "wireguard-client",
                  "wireguard_client_mode": "file", "wireguard_client_configuration_file": WG_CONF,
                  "wireguard_client_configuration_filename": "ca.conf", "ip_subnet": "10.75.0.2/32", "enabled": False,
                  "firewall_zone_id": z_ext},
        },
        "firewall/zone": {
            z_int: {"_id": z_int, "name": "Internal", "zone_key": "internal", "default_zone": True, "network_ids": [lan]},
            z_ext: {"_id": z_ext, "name": "External", "zone_key": "external", "default_zone": True, "network_ids": [wan, vpn]},
            z_iot: {"_id": z_iot, "name": "IoT", "zone_key": None, "default_zone": False, "network_ids": [iot]},
        },
        "firewall-policies": {
            i(30): {"_id": i(30), "name": "Allow Return Traffic", "predefined": True, "action": "ALLOW", "index": 1,
                    "source": {"zone_id": z_int}, "destination": {"zone_id": z_ext}},
            i(31): {"_id": i(31), "name": "IoT to DNS", "predefined": False, "action": "ALLOW", "index": 10002, "protocol": "all",
                    "enabled": True, "source": {"zone_id": z_iot, "matching_target": "ANY"},
                    "destination": {"zone_id": z_int, "matching_target": "ANY", "port": "53"}},
            i(32): {"_id": i(32), "name": "Block vacuum", "predefined": False, "action": "BLOCK", "index": 10001,
                    "protocol": "all", "enabled": True,
                    "source": {"zone_id": z_int, "matching_target": "CLIENT", "client_macs": ["24:9e:7d:d2:9d:cd"]},
                    "destination": {"zone_id": z_ext, "matching_target": "ANY"}},
        },
        "trafficroutes": {
            i(40): {"_id": i(40), "description": "Mullvad CA", "enabled": True, "matching_target": "INTERNET",
                    "network_id": vpn, "kill_switch_enabled": True, "target_devices": [{"network_id": lan, "type": "NETWORK"}]},
        },
        "apgroups": {apg: {"_id": apg, "name": "All APs", "attr_hidden_id": "default", "attr_no_delete": True, "device_macs": []}},
        "usergroup": {ug: {"_id": ug, "name": "Default", "attr_hidden_id": "Default", "attr_no_delete": True}},
        "wlanconf": {
            i(50): {"_id": i(50), "name": "Mike's IoT", "security": "wpapsk", "wpa_mode": "wpa2", "x_passphrase": WIFI_PW,
                    "x_iapp_key": "deadbeef", "networkconf_id": iot, "ap_group_ids": [apg], "usergroup_id": ug,
                    "wlan_bands": ["2g"], "enabled": True},
        },
        "user": {
            i(60): {"_id": i(60), "mac": "ec:b5:fa:17:e2:72", "name": "Hue Bridge", "use_fixedip": True,
                    "fixed_ip": "192.168.2.20", "network_id": iot, "noted": True, "note": "living room"},
            i(61): {"_id": i(61), "mac": "aa:aa:aa:aa:aa:01", "hostname": "nobody"},      # not customised
        },
        "device": {
            i(70): {"_id": i(70), "mac": "1c:0b:8b:18:fa:88", "model": "USW", "type": "usw", "name": "Office switch",
                    "ip": "10.42.0.5", "state": 1,
                    "port_overrides": [{"port_idx": 3, "name": "NAS", "native_networkconf_id": iot}],
                    "port_table": [
                        {"port_idx": 1, "up": True, "speed": 2500, "full_duplex": True, "is_uplink": True},
                        {"port_idx": 2, "up": True, "speed": 1000, "full_duplex": True, "port_poe": True,
                         "poe_power": "6.5", "poe_mode": "auto", "rx_errors": 4200},
                        {"port_idx": 3, "up": True, "speed": 1000, "full_duplex": True},
                        {"port_idx": 4, "up": False}]},
        },
        "setting": {
            i(80): {"_id": i(80), "key": "mgmt", "x_ssh_password": "ssh-pw", "x_mgmt_key": "console-made", "led_enabled": False},
            i(81): {"_id": i(81), "key": "country", "code": 840},
        },
    }


def fresh(seed=0xb2):
    """A factory-reset console: built-ins only, new ids, the device adopted but unnamed."""
    i = lambda n: _id(n, seed)
    lan, wan = i(1), i(2)
    return {
        "networkconf": {
            lan: {"_id": lan, "name": "Default", "purpose": "corporate", "attr_hidden_id": "LAN", "attr_no_delete": True,
                  "ip_subnet": "192.168.1.1/24", "dhcpd_enabled": True, "dhcpd_start": "192.168.1.6", "dhcpd_stop": "192.168.1.254"},
            wan: {"_id": wan, "name": "Internet 1", "purpose": "wan", "attr_hidden_id": "WAN", "attr_no_delete": True,
                  "wan_networkgroup": "WAN", "wan_type": "dhcp"},
        },
        "firewall/zone": {
            i(10): {"_id": i(10), "name": "Internal", "zone_key": "internal", "default_zone": True, "network_ids": [lan]},
            i(11): {"_id": i(11), "name": "External", "zone_key": "external", "default_zone": True, "network_ids": [wan]},
        },
        "firewall-policies": {i(30): {"_id": i(30), "name": "Allow Return Traffic", "predefined": True}},
        "trafficroutes": {},
        "apgroups": {i(20): {"_id": i(20), "name": "All APs", "attr_hidden_id": "default", "attr_no_delete": True}},
        "usergroup": {i(21): {"_id": i(21), "name": "Default", "attr_hidden_id": "Default", "attr_no_delete": True}},
        "wlanconf": {},
        "user": {},
        "device": {i(70): {"_id": i(70), "mac": "1c:0b:8b:18:fa:88", "model": "USW", "type": "usw"}},
        "setting": {i(80): {"_id": i(80), "key": "mgmt", "x_mgmt_key": "new-console-key", "led_enabled": True},
                    i(81): {"_id": i(81), "key": "country", "code": 840}},
    }


def find_bash():
    """A real bash: Git for Windows' on Windows (not WSL's), /bin/bash elsewhere."""
    for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(p):
            return p
    if os.name != "nt":
        return shutil.which("bash")
    return None


def run_script(text, port, stdin, timeout=180):
    """Run a generated restore script against a mock console on `port`, answering prompts from `stdin`."""
    text = text.replace("UNIFI='https://127.0.0.1';", f"UNIFI='https://127.0.0.1:{port}';", 1)
    d = tempfile.mkdtemp()
    path = os.path.join(d, "restore.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        # Bytes, not text: text mode on Windows would turn each newline in the answers into CR LF.
        r = subprocess.run([find_bash(), path], input=stdin.encode("utf-8"), capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    finally:
        shutil.rmtree(d, ignore_errors=True)
