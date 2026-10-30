"""
Optional Nmap scans (if Nmap is installed) for the device detective and the
security scorecard.

Only TCP connect scans (-sT), which need no administrator rights on Windows,
macOS or Linux. Targets must be private (LAN) addresses; the caller also checks
that each one is a client the UniFi console knows about, so the AI can't be
talked into scanning anything else. No shell: arguments are passed as a list.

Install: Windows https://nmap.org/download (the installer also offers Npcap),
macOS the nmap.org .dmg or `brew install nmap`, Linux `sudo apt install nmap`.
"""

import ipaddress
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

# Common places Nmap lands when it isn't on PATH.
_CANDIDATES = [r"C:\Program Files (x86)\Nmap\nmap.exe", r"C:\Program Files\Nmap\nmap.exe",
               "/opt/homebrew/bin/nmap", "/usr/local/bin/nmap", "/usr/bin/nmap"]

# Ports that matter for security on a home/small-office LAN, with why.
RISKY_PORTS = {
    21: "FTP: passwords travel unencrypted",
    23: "Telnet: unencrypted remote login, a favourite of IoT malware",
    2323: "Telnet (alternate port), typical of IoT malware",
    445: "SMB file sharing: a common ransomware path",
    139: "NetBIOS file sharing",
    3389: "Remote Desktop",
    5900: "VNC remote screen, often without a password",
    1883: "MQTT without TLS: smart-home messages readable and writable",
    554: "RTSP camera stream, often without a password",
    8080: "HTTP admin page on a non-standard port",
    80: "HTTP admin page without encryption",
    9100: "Raw printing port (anyone can print or read jobs)",
    161: "SNMP (TCP)",
    3306: "MySQL database",
    5432: "PostgreSQL database",
    6379: "Redis, usually without a password",
    27017: "MongoDB, often without a password",
}
IDENTIFY_TIMEOUT = 150
SWEEP_TIMEOUT = 300
MAX_SWEEP_HOSTS = 128


class ScanError(Exception):
    pass


def find():
    p = shutil.which("nmap")
    if p:
        return p
    return next((c for c in _CANDIDATES if os.path.exists(c)), None)


def info():
    """-> {"installed": bool, "path", "version"}"""
    p = find()
    if not p:
        return {"installed": False, "path": None, "version": None}
    try:
        out = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=15).stdout
        ver = out.split("\n", 1)[0].replace("Nmap version", "").split("(")[0].strip()
    except (OSError, subprocess.SubprocessError):
        ver = None
    return {"installed": True, "path": p, "version": ver}


def check_target(ip):
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        raise ScanError(f"'{ip}' is not an IP address.")
    if not (a.is_private or a.is_link_local) or a.is_loopback or a.is_multicast or a.is_unspecified:
        raise ScanError(f"{a} isn't a private LAN address; only devices on your own network can be scanned.")
    return str(a)


def _run(args, timeout):
    p = find()
    if not p:
        raise ScanError("Nmap isn't installed. Get it from nmap.org (Windows or Mac installer), "
                        "or on a Mac: brew install nmap.")
    try:
        r = subprocess.run([p] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ScanError("The scan took too long and was stopped.")
    except OSError as e:
        raise ScanError(f"Couldn't start Nmap: {e}")
    if r.returncode != 0 and "<nmaprun" not in r.stdout:
        raise ScanError(f"Nmap failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _parse(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ScanError(f"Couldn't read Nmap's output ({e}).")
    hosts = []
    for h in root.findall("host"):
        addr = next((a.get("addr") for a in h.findall("address") if a.get("addrtype") in ("ipv4", "ipv6")), None)
        mac = next((a.get("addr") for a in h.findall("address") if a.get("addrtype") == "mac"), None)
        status = (h.find("status").get("state") if h.find("status") is not None else "")
        ports = []
        for pt in h.findall("ports/port"):
            st = pt.find("state")
            if st is None or st.get("state") != "open":
                continue
            sv = pt.find("service")
            svc = {k: sv.get(k) for k in ("name", "product", "version", "extrainfo", "ostype", "devicetype")
                   if sv is not None and sv.get(k)}
            num = int(pt.get("portid"))
            ports.append(dict(svc, port=num, protocol=pt.get("protocol"),
                              risk=RISKY_PORTS.get(num)))
        hostnames = [n.get("name") for n in h.findall("hostnames/hostname") if n.get("name")]
        hosts.append({"ip": addr, "mac": mac, "status": status, "hostnames": hostnames, "open_ports": ports,
                      "timed_out": h.get("timedout") == "true"})
    return hosts


def _is_v6(ip):
    return ipaddress.ip_address(ip).version == 6


def identify(ip):
    """Service/version scan of one device's 200 most common TCP ports (about 30-90 s)."""
    ip = check_target(ip)
    out = _run(["-sT", "-sV", "--version-light", "-Pn", "-T4", "--top-ports", "200"]
               + (["-6"] if _is_v6(ip) else [])
               + ["--host-timeout", f"{IDENTIFY_TIMEOUT - 10}s", "-oX", "-", ip], IDENTIFY_TIMEOUT)
    hosts = _parse(out)
    return hosts[0] if hosts else {"ip": ip, "open_ports": [], "status": "unknown"}


def sweep(ips):
    """Check many devices for the risky ports only (fast). One Nmap run covers IPv4 only (Nmap
    needs -6 for IPv6 and can't mix them), at most MAX_SWEEP_HOSTS devices.
    -> {"hosts": devices with risky ports open, "requested", "submitted" (sent to Nmap),
        "skipped", "skipped_reason", "timed_out": [ips Nmap gave up on: unknown, not clean]}"""
    ips = list(dict.fromkeys(check_target(i) for i in ips))
    v4 = [i for i in ips if not _is_v6(i)]
    submitted = v4[:MAX_SWEEP_HOSTS]
    reasons = []
    if len(v4) > MAX_SWEEP_HOSTS:
        reasons.append(f"only the first {MAX_SWEEP_HOSTS} are scanned at once")
    if len(v4) < len(ips):
        reasons.append("IPv6-only addresses aren't swept")
    res = {"hosts": [], "requested": len(ips), "submitted": len(submitted), "skipped": len(ips) - len(submitted),
           "skipped_reason": "; ".join(reasons), "timed_out": []}
    if not submitted:
        return res
    ports = ",".join(str(p) for p in sorted(RISKY_PORTS))
    # No --open: every host comes back, so a device Nmap never reported on (or gave up on) is
    # known to be unchecked instead of silently looking clean.
    out = _run(["-sT", "-Pn", "-T4", "-p", ports, "--host-timeout", "60s", "-oX", "-"] + submitted,
               SWEEP_TIMEOUT)
    hosts = _parse(out)
    finished = {h["ip"] for h in hosts if not h.get("timed_out")}
    res["timed_out"] = [ip for ip in submitted if ip not in finished]
    res["hosts"] = [h for h in hosts if h["open_ports"]]      # open ports seen before a timeout still count
    return res
