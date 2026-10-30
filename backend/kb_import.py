"""
Bulk imports into the knowledge library (Tools → Knowledge library → Import):

  * Ubiquiti Help Center: every published English article, through the help center's
    public API (help.ui.com/api/v2/help_center), saved as HTML with its source address.
  * Ubiquiti documents: datasheets, user guides, installation guides and quick start
    guides listed on Ubiquiti's download site (download.svc.ui.com/v1/downloads), as PDF.
    UniFi products only by default; optionally every product line (UISP, airMAX, ...).
  * Networking RFCs: a curated set of the IETF standards a home or office network runs on
    (IP, ARP, ICMP, DHCP, DNS, IPv6, multicast, NAT, QoS, RADIUS/EAP, IPsec, routing, time,
    management, security BCPs), as text, each headed with its status, what it obsoletes /
    updates, what updated it, and its errata page (from the RFC Editor's metadata).
  * Security guidance: NIST publications (WLAN, firewalls, IPsec VPNs, DNS, IPv6, zero
    trust, IoT, patching) and NIST's small-business network guidance. (CISA refuses
    automated downloads; add its pages yourself.)

Ubiquiti's documents are copyrighted: they are downloaded to this computer for your own
reference and never leave it (data/ is not committed or shared). RFCs (IETF Trust terms)
and NIST/CISA publications (US Government works) may be shared; see knowledge.export_package.
Deliberately not imported: IEEE standards (IEEE forbids redistribution), and crawls of
vendor, Wi-Fi Alliance or community sites (copyrighted, some forbid scraping); add single
pages you need with "Add page", and they are tagged with the right authority.

Each import runs in the background, one request at a time with a short pause, and remembers
what it saved (data/knowledge_sources.json), so running it again fetches only what is new
or changed.
"""

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import config_store as cfg
import knowledge

UA = {"User-Agent": "Mozilla/5.0 (unifi-flinchbot knowledge library)"}
HELP_API = "https://help.ui.com/api/v2/help_center/en-us/articles.json?per_page=100&page={page}"
DOWNLOADS_API = "https://download.svc.ui.com/v1/downloads?page={page}"
DOC_CATEGORIES = {"Datasheets": "Datasheet", "User Guides": "User Guide", "Installation Guides": "Install Guide",
                  "Quick Start Guides": "Quick Start"}
RFC_URL = "https://www.rfc-editor.org/rfc/rfc{n}.txt"
RFC_META = "https://www.rfc-editor.org/rfc/rfc{n}.json"
PAUSE = 0.25                     # seconds between requests: be a polite guest
MAX_DOC = 40_000_000

# The standards a home/office network actually runs on. (802.1Q VLANs, 802.11 Wi-Fi and
# 802.3af/at PoE are IEEE standards, not RFCs, and aren't freely downloadable.)
RFCS = [
    (791, "Internet Protocol (IPv4)"), (792, "Internet Control Message Protocol (ICMP)"),
    (826, "Address Resolution Protocol (ARP)"), (768, "User Datagram Protocol (UDP)"),
    (9293, "Transmission Control Protocol (TCP)"), (1812, "Requirements for IPv4 Routers"),
    (1918, "Private IPv4 Address Space"), (6598, "Shared Address Space for Carrier-Grade NAT"),
    (4632, "Classless Inter-Domain Routing (CIDR)"), (1191, "Path MTU Discovery"),
    (2474, "Differentiated Services Field (DSCP)"),
    (2131, "Dynamic Host Configuration Protocol (DHCP)"), (2132, "DHCP Options"),
    (3046, "DHCP Relay Agent Information Option (Option 82)"), (8415, "DHCP for IPv6 (DHCPv6)"),
    (8200, "Internet Protocol Version 6 (IPv6)"), (4291, "IPv6 Addressing Architecture"),
    (4443, "ICMPv6"), (4861, "IPv6 Neighbor Discovery"), (4862, "IPv6 Stateless Address Autoconfiguration (SLAAC)"),
    (4193, "IPv6 Unique Local Addresses (ULA)"), (8106, "IPv6 Router Advertisement DNS Options"),
    (8981, "IPv6 Temporary Addresses (Privacy Extensions)"), (7217, "IPv6 Stable Privacy Addresses"),
    (7084, "Basic Requirements for IPv6 Customer Edge Routers"), (8201, "Path MTU Discovery for IPv6"),
    (8305, "Happy Eyeballs Version 2 (IPv4/IPv6 Fallback)"),
    (1034, "DNS Concepts and Facilities"), (1035, "DNS Implementation and Specification"),
    (8484, "DNS over HTTPS (DoH)"), (7858, "DNS over TLS (DoT)"), (6762, "Multicast DNS (mDNS)"),
    (6763, "DNS-Based Service Discovery (DNS-SD)"),
    (3022, "Traditional NAT"), (4787, "NAT Behavioral Requirements for UDP"), (6146, "Stateful NAT64"),
    (6886, "NAT Port Mapping Protocol (NAT-PMP)"), (6887, "Port Control Protocol (PCP)"),
    (2865, "RADIUS"), (2866, "RADIUS Accounting"), (3580, "IEEE 802.1X RADIUS Usage Guidelines"),
    (3748, "Extensible Authentication Protocol (EAP)"), (5216, "EAP-TLS Authentication Protocol"),
    (4301, "Security Architecture for IP (IPsec)"), (7296, "Internet Key Exchange v2 (IKEv2)"),
    (5798, "Virtual Router Redundancy Protocol (VRRP) v3"), (4271, "Border Gateway Protocol 4 (BGP-4)"),
    (2328, "OSPF Version 2"), (5905, "Network Time Protocol v4 (NTP)"), (5424, "The Syslog Protocol"),
    (3411, "SNMP Architecture"),
    # ARP-related, multicast, QoS
    (5227, "IPv4 Address Conflict Detection"), (3927, "IPv4 Link-Local Addresses (169.254/16)"),
    (1112, "Host Extensions for IP Multicasting"), (3376, "IGMP Version 3"), (3810, "Multicast Listener Discovery v2 (MLDv2)"),
    (4541, "IGMP and MLD Snooping Switches"), (4594, "DiffServ Service Classes"),
    (8325, "Mapping DiffServ to IEEE 802.11 (Wi-Fi QoS)"),
    # more DNS, IPsec, time, management
    (4033, "DNS Security (DNSSEC) Introduction"), (4303, "IP Encapsulating Security Payload (ESP)"),
    (8915, "Network Time Security (NTS)"), (3414, "SNMPv3 User-based Security Model"), (5426, "Syslog over UDP"),
    # security best current practice
    (2827, "Network Ingress Filtering (BCP 38)"), (3704, "Ingress Filtering for Multihomed Networks (BCP 84)"),
    (4890, "Recommendations for Filtering ICMPv6 in Firewalls"), (6092, "Simple Security in IPv6 Customer Equipment"),
    (8504, "IPv6 Node Requirements"), (9099, "Operational Security Considerations for IPv6"),
    (7454, "BGP Operations and Security"),
]

# US Government works (public domain in the US): curated for small and medium networks.
# CISA's site refuses automated downloads (403), so its edge-device guidance isn't fetched here;
# save those pages in a browser and add them with Add files (they count as shareable guidance).
SECURITY_DOCS = [
    ("NIST SP 800-153 - Guidelines for Securing Wireless LANs (2012)",
     "https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-153.pdf"),
    ("NIST SP 800-41r1 - Guidelines on Firewalls and Firewall Policy (2009)",
     "https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-41r1.pdf"),
    ("NIST SP 800-97 - Establishing Wireless Robust Security Networks, 802.11i (2007)",
     "https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-97.pdf"),
    ("NIST SP 800-77r1 - Guide to IPsec VPNs (2020)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-77r1.pdf"),
    ("NIST SP 800-207 - Zero Trust Architecture (2020)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-207.pdf"),
    ("NIST SP 800-189 - Resilient Interdomain Traffic Exchange, BGP and DDoS (2019)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-189.pdf"),
    ("NIST SP 800-119 - Guidelines for the Secure Deployment of IPv6 (2010)",
     "https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-119.pdf"),
    ("NIST SP 800-81-2 - Secure Domain Name System (DNS) Deployment Guide (2013)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-81-2.pdf"),
    ("NIST SP 800-46r2 - Enterprise Telework, Remote Access and BYOD Security (2016)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-46r2.pdf"),
    ("NIST SP 800-40r4 - Enterprise Patch Management Planning (2022)",
     "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-40r4.pdf"),
    ("NIST IR 8259 - IoT Device Cybersecurity Baseline for Manufacturers (2020)",
     "https://nvlpubs.nist.gov/nistpubs/ir/2020/NIST.IR.8259.pdf"),
    ("NIST - Securing Network Connections (Small Business)",
     "https://www.nist.gov/itl/smallbusinesscyber/guidance-topic/securing-network-connections"),
]

SOURCES = {
    "ui-help": "Ubiquiti Help Center articles",
    "ui-docs": "Ubiquiti datasheets and guides",
    "rfc": "Networking RFCs",
    "security": "Security guidance (NIST)",
    "feeds": "Feeds",
}
FEED_ITEMS = 100                 # newest items kept per feed
# Offered as one-click subscriptions in the app (checked to be real RSS feeds).
SUGGESTED_FEEDS = [
    ("UniFi Network release notes",
     "https://community.ui.com/rss/releases/UniFi-Network-Controller/e6712595-81bb-4829-8e42-9e2630fabcfe"),
    ("Packet Pushers blog", "https://feeds.packetpushers.net/packetpushersblog/"),
]

_job = {"running": False, "source": None, "label": None, "done": 0, "total": 0, "added": 0, "updated": 0,
        "skipped": 0, "failed": 0, "web_only": 0, "errors": [], "error": None, "finished_at": None, "cancel": False}
_lock = threading.Lock()


def _manifest():
    return knowledge.sources()


def _save_manifest(m):
    knowledge.save_sources(m)


def _get(url, limit=MAX_DOC, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=cfg.ssl_context()))
    with opener.open(req, timeout=timeout) as r:
        data = r.read(limit + 1)
        ctype = (r.headers.get("Content-Type") or "").lower()
    if len(data) > limit:
        raise knowledge.KnowledgeError("too big")
    return data, ctype


def _get_json(url):
    return json.loads(_get(url, 50_000_000)[0])


def _file_name(prefix, title, ext):
    stem = re.sub(r"[®™©]", "", f"{prefix} - {title}")
    stem = re.sub(r"[^\w\-. ()]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:150]
    return stem + ext


def _write(name, data):
    os.makedirs(knowledge.folder(), exist_ok=True)
    tmp = os.path.join(knowledge.folder(), "." + name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(knowledge.folder(), name))


# ── the sources: each yields (file name, source url, version, fetch() -> bytes) ─
def _help_items(job):
    page, items = 1, []
    while True:
        d = _get_json(HELP_API.format(page=page))
        for a in d.get("articles", []):
            if a.get("draft") or not a.get("body"):
                continue
            items.append(a)
        job["total"] = d.get("count") or len(items)
        if not d.get("next_page") or job["cancel"]:
            break
        page += 1
        time.sleep(PAUSE)
    for a in items:
        title = (a.get("title") or a.get("name") or f"Article {a['id']}").strip()

        def fetch(a=a, title=title):
            # The API already returned the article body: no second request needed.
            body = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title></head>"
                    f"<body><!-- source: {html.escape(a.get('html_url') or '')} -->\n<h1>{html.escape(title)}</h1>\n"
                    f"{a['body']}</body></html>")
            return body.encode("utf-8")
        yield (_file_name("UI Help", title, ".html"), a.get("html_url"), a.get("updated_at") or a.get("edited_at"), fetch)


def _doc_items(job, all_lines=False):
    first = _get_json(DOWNLOADS_API.format(page=1))
    pages = int((first.get("pagination") or {}).get("totalPages") or 1)
    seen, items = set(), []
    for p in range(1, pages + 1):
        d = first if p == 1 else _get_json(DOWNLOADS_API.format(page=p))
        for x in d.get("downloads", []):
            kind = DOC_CATEGORIES.get((x.get("category") or {}).get("name"))
            url = x.get("file_path") or x.get("file_url") or ""
            if not kind or not url.lower().startswith("http") or url in seen:
                continue
            if not all_lines and "unifi" not in (x.get("product_lines") or []):
                continue
            seen.add(url)
            if not url.lower().split("?")[0].endswith(".pdf"):
                job["web_only"] += 1          # an interactive page-flip viewer: no text to read
                continue
            items.append((kind, x, url))
        if job["cancel"]:
            break
        time.sleep(PAUSE)
    job["total"] = len(items)
    for kind, x, url in items:
        name = re.sub(r"\s+", " ", x.get("name") or os.path.basename(url)).strip()
        yield (_file_name(f"UI {kind}", name, ".pdf"), url, x.get("date_published") or "",
               lambda url=url: _get(url)[0])


def _feeds_path():
    return os.path.join(cfg.DATA_DIR, "knowledge_feeds.json")


def feeds():
    try:
        with open(_feeds_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_feeds(items):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    with open(_feeds_path(), "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)


def parse_feed(data):
    """RSS 2.0 or Atom -> (feed title, [{title, link, date, id, html}]), newest first as the feed lists them."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise knowledge.KnowledgeError("That address isn't an RSS or Atom feed.")
    atom, content = "{http://www.w3.org/2005/Atom}", "{http://purl.org/rss/1.0/modules/content/}encoded"
    text = lambda el, tag: ((el.find(tag).text or "") if el.find(tag) is not None else "").strip()  # noqa: E731
    if root.tag == "rss" or root.find("channel") is not None:
        ch = root.find("channel")
        items = [{"title": text(i, "title"), "link": text(i, "link"), "date": text(i, "pubDate"),
                  "id": text(i, "guid") or text(i, "link"),
                  "html": text(i, content) or text(i, "description")} for i in ch.findall("item")]
        return text(ch, "title"), items
    if root.tag == atom + "feed":
        out = []
        for e in root.findall(atom + "entry"):
            link = next((l.get("href") for l in e.findall(atom + "link") if l.get("rel") in (None, "alternate")), "")
            out.append({"title": text(e, atom + "title"), "link": link or "",
                        "date": text(e, atom + "updated") or text(e, atom + "published"), "id": text(e, atom + "id") or link,
                        "html": text(e, atom + "content") or text(e, atom + "summary")})
        return text(root, atom + "title"), out
    raise knowledge.KnowledgeError("That address isn't an RSS or Atom feed.")


def add_feed(url, title=None):
    """Subscribe: check the address really is a feed. -> the feed list."""
    url = str(url or "").strip()
    if not re.match(r"^https?://\S+$", url, re.I):
        raise knowledge.KnowledgeError("That isn't a web address (it should start with http:// or https://).")
    items = feeds()
    if any(f["url"] == url for f in items):
        raise knowledge.KnowledgeError("You're already subscribed to that feed.")
    try:
        name, entries = parse_feed(_get(url, 20_000_000, timeout=30)[0])
    except urllib.error.URLError as e:
        raise knowledge.KnowledgeError(f"Couldn't download that feed ({getattr(e, 'reason', e)}).")
    items.append({"url": url, "title": (title or name or url)[:120], "items": len(entries)})
    _save_feeds(items)
    return items


def remove_feed(url, delete_items=True):
    items = [f for f in feeds() if f["url"] != url]
    _save_feeds(items)
    if delete_items:                                     # its articles leave the library too
        m = _manifest()
        for name, v in list(m.items()):
            if v.get("feed") == url:
                try:
                    os.remove(os.path.join(knowledge.folder(), name))
                except OSError:
                    pass
                del m[name]
        _save_manifest(m)
        knowledge.refresh()
    return items


def _feed_items(job):
    subs = feeds()
    if not subs:
        raise knowledge.KnowledgeError("No feeds yet. Subscribe to one first.")
    collected = []
    for f in subs:
        if job["cancel"]:
            break
        try:
            title, entries = parse_feed(_get(f["url"], 20_000_000, timeout=30)[0])
        except Exception as e:
            job["failed"] += 1
            job["errors"].append(f"{f['title']}: {e}")
            continue
        for e in entries[:FEED_ITEMS]:
            if e["title"] and e["html"]:
                collected.append((f, e))
        time.sleep(PAUSE)
    job["total"] = len(collected)
    for f, e in collected:
        def fetch(f=f, e=e):
            body = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(e['title'])}</title></head>"
                    f"<body><!-- source: {html.escape(e['link'])} -->\n<h1>{html.escape(e['title'])}</h1>\n"
                    f"<p>{html.escape(f['title'])} · {html.escape(e['date'])}</p>\n{e['html']}</body></html>")
            return body.encode("utf-8")
        yield (_file_name(f"Feed {f['title'][:40]}", e["title"], ".html"), e["link"], e["date"] or e["id"], fetch,
               {"feed": f["url"]})


def rfc_header(n, meta):
    """A plain-text header with the RFC's status and relationships, so the AI knows whether
    it is current (and which RFC replaced or extended it)."""
    j = lambda k: ", ".join(meta.get(k) or []) or "none"     # noqa: E731
    lines = [f"RFC {n}: {meta.get('title') or ''}".rstrip(),
             f"Status: {meta.get('status') or 'unknown'} · Published: {meta.get('pub_date') or 'unknown'}",
             f"Obsoletes: {j('obsoletes')} · Obsoleted by: {j('obsoleted_by')}",
             f"Updates: {j('updates')} · Updated by: {j('updated_by')}"]
    if meta.get("errata_url"):
        lines.append(f"Errata: {meta['errata_url']}")
    if meta.get("obsoleted_by"):
        lines.append(f"NOTE: this RFC is obsolete. Prefer {j('obsoleted_by')}.")
    return "\n".join(lines) + "\n" + "=" * 72 + "\n\n"


def _rfc_items(job):
    job["total"] = len(RFCS)
    for n, title in RFCS:
        try:
            meta = _get_json(RFC_META.format(n=n))
        except Exception:
            meta = {}
        version = json.dumps([meta.get("status"), meta.get("obsoleted_by"), meta.get("updated_by")])

        def fetch(n=n, meta=meta):
            return rfc_header(n, meta).encode("utf-8") + _get(RFC_URL.format(n=n), 5_000_000)[0]
        yield (_file_name(f"RFC {n}", title, ".txt"), RFC_URL.format(n=n), version, fetch)


def _security_items(job):
    job["total"] = len(SECURITY_DOCS)
    for title, url in SECURITY_DOCS:
        ext = ".pdf" if url.lower().endswith(".pdf") else ".html"

        def fetch(url=url, title=title, ext=ext):
            data, ctype = _get(url)
            if ext == ".html":                      # keep the page's own text, say where it came from
                data = f"<!-- source: {html.escape(url)} -->\n<title>{html.escape(title)}</title>\n".encode() + data
            return data
        yield (_file_name("Security", title, ext), url, "1", fetch)


# ── running an import ─────────────────────────────────────────────────────────
def start(source, all_lines=False):
    """Start an import in the background. -> True, or False if one is already running."""
    if source not in SOURCES:
        raise knowledge.KnowledgeError("Unknown source.")
    with _lock:
        if _job["running"]:
            return False
        _job.update(running=True, source=source, label=SOURCES[source], done=0, total=0, added=0, updated=0,
                    skipped=0, failed=0, web_only=0, errors=[], error=None, finished_at=None, cancel=False)
    threading.Thread(target=_run, args=(source, all_lines), name=f"kb-import-{source}", daemon=True).start()
    return True


def cancel():
    _job["cancel"] = True


def status():
    return {k: v for k, v in _job.items() if k != "cancel"}


def _run(source, all_lines):
    job = _job
    try:
        items = {"ui-help": _help_items, "ui-docs": lambda j: _doc_items(j, all_lines), "rfc": _rfc_items,
                 "security": _security_items, "feeds": _feed_items}[source](job)
        manifest = _manifest()
        for name, url, version, fetch, *extra in items:
            if job["cancel"]:
                break
            prev = manifest.get(name)
            exists = os.path.isfile(os.path.join(knowledge.folder(), name))
            if exists and prev and prev.get("version") == version:
                job["skipped"] += 1
            else:
                try:
                    data = fetch()
                    if name.endswith(".pdf") and not data.startswith(b"%PDF"):
                        raise knowledge.KnowledgeError("not a PDF")
                    _write(name, data)
                    manifest[name] = dict({"source": url, "version": version, "from": source,
                                           "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}, **(extra[0] if extra else {}))
                    job["updated" if exists else "added"] += 1
                    if not name.endswith(".html"):          # help articles came with the listing
                        time.sleep(PAUSE)
                except Exception as e:
                    job["failed"] += 1
                    if len(job["errors"]) < 20:
                        job["errors"].append(f"{name}: {e}")
            job["done"] += 1
            if job["done"] % 25 == 0:
                _save_manifest(manifest)
        _save_manifest(manifest)
        job["phase"] = "reading"
        knowledge.refresh()                                  # index what arrived
    except urllib.error.URLError as e:
        job["error"] = f"Couldn't reach {source} ({getattr(e, 'reason', e)}). Is this computer online?"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job.update(running=False, finished_at=time.time())


def imported():
    """-> {source: number of files from it still in the library}"""
    m, out = _manifest(), {k: 0 for k in SOURCES}
    for name, v in m.items():
        if os.path.isfile(os.path.join(knowledge.folder(), name)):
            out[v.get("from")] = out.get(v.get("from"), 0) + 1
    return out
