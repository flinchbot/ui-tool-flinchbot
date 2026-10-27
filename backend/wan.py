"""
WAN health over time: speed tests (the console's own history plus tests this
app ran) and internet/WAN events from the system log, drawn as an SVG.

Speed tests can run on a schedule per connection (off by default). The
scheduler only runs for the active connection and never while UniFi sign-in
is blocked, so it can't add to a lockout.
"""

import html
import threading
import time
from datetime import datetime

from unifi_client import UniFiError, summarize_event, to_ms

SCHEDULE_CHOICES = (0, 1, 2, 3, 4, 6, 8, 12, 24, 48, 168)
_run_lock = threading.Lock()


# ── running a test ────────────────────────────────────────────────────────────
def _point(st, source):
    def num(v):
        return round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"at": to_ms(st.get("rundate") or st.get("time")), "download_mbps": num(st.get("xput_download")),
            "upload_mbps": num(st.get("xput_upload")), "latency_ms": num(st.get("latency")), "source": source}


POLL_SECONDS = 5


def run_speedtest(client, timeout=150, poll=None, sleep=time.sleep):
    """Start a test on the gateway and wait for a NEW result. One test at a time app-wide.
    -> {at, download_mbps, upload_mbps, latency_ms, source}"""
    poll = POLL_SECONDS if poll is None else poll
    if not _run_lock.acquire(blocking=False):
        raise UniFiError("A speed test is already running. Wait for it to finish.", "busy")
    try:
        before = client.get_speedtest_status().get("rundate")
        client.speedtest()
        deadline = time.time() + timeout
        while time.time() < deadline:
            sleep(poll)
            st = client.get_speedtest_status()
            done = st.get("rundate") and st.get("rundate") != before and st.get("xput_download") is not None \
                and 1 not in (st.get("status_download"), st.get("status_upload"), st.get("status_ping"))
            if done:
                return _point(st, "app")
        raise UniFiError(f"The speed test didn't finish within {timeout} seconds. The gateway may still "
                         f"be running it; check again in a minute.", "timeout")
    finally:
        _run_lock.release()


def is_running():
    return _run_lock.locked()


# ── history ───────────────────────────────────────────────────────────────────
def collect(client, days, local_points):
    """-> (points newest-last, events newest-last, notes). Console history and the app's
    own records are merged; two results within 2 minutes are the same test."""
    now = int(time.time() * 1000)
    start = now - int(days * 86400 * 1000)
    notes, points = [], []
    try:
        points = [_point(r, "console") for r in client.get_speedtest_history(start, now)]
    except UniFiError as e:
        notes.append(f"The console's speed-test history isn't available ({e}).")
    for p in local_points:
        if p.get("download_mbps") is None and p.get("latency_ms") is None:
            continue                      # a failed run: kept in the file, not a data point
        if (p.get("at") or 0) >= start and not any(abs((q["at"] or 0) - p["at"]) < 120_000 for q in points):
            points.append({k: p.get(k) for k in ("at", "download_mbps", "upload_mbps", "latency_ms")} | {"source": "app"})
    points = sorted((p for p in points if p.get("at")), key=lambda p: p["at"])
    events = []
    try:
        for e in client.get_events(300, max(1, int(days * 24)), ["INTERNET_AND_WAN"]):
            s = summarize_event(e)
            if s["at"] and s["at"] >= start:
                events.append(s)
    except UniFiError as e:
        notes.append(f"WAN events from the system log aren't available ({e}).")
    events.sort(key=lambda e: e["at"])
    return points, events, notes


def is_outage(ev):
    t = f"{ev.get('key') or ''} {ev.get('title') or ''}".lower()
    return any(w in t for w in ("down", "offline", "disconnect", "lost", "fail", "outage"))


def summary(points):
    def stats(key):
        vals = [p[key] for p in points if p.get(key) is not None]
        if not vals:
            return None
        s = sorted(vals)
        return {"min": s[0], "median": s[len(s) // 2], "max": s[-1], "latest": vals[-1]}
    return {"tests": len(points), "download_mbps": stats("download_mbps"), "upload_mbps": stats("upload_mbps"),
            "latency_ms": stats("latency_ms")}


# ── SVG chart ─────────────────────────────────────────────────────────────────
# Dark surface, validated categorical slots 1-2 (download, upload) and slot 3 for
# latency; status critical/warning for events. One y-axis per chart: throughput and
# latency are different units, so they are two charts on a shared time axis.
S = {"surface": "#1a1a19", "grid": "#2e2e2b", "text": "#f0efec", "text2": "#c3c2b7", "muted": "#8c8b84",
     "down": "#3987e5", "up": "#d95926", "lat": "#199e70", "critical": "#d03b3b", "warning": "#fab219"}
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
W, ML, MR, MT = 1000, 64, 24, 92
PLOT_H, GAP = 210, 70


def _e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _nice_max(v):
    if v <= 0:
        return 1
    mag = 10 ** (len(str(int(v))) - 1)
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _fmt_time(ms, days):
    d = datetime.fromtimestamp(ms / 1000)
    return d.strftime("%H:%M") if days <= 1 else (d.strftime("%a %d") if days <= 7 else d.strftime("%d %b"))


def _fmt_full(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%a %d %b %Y, %H:%M")


def render_svg(points, events, days, network=None):
    now = int(time.time() * 1000)
    x0_ms, x1_ms = now - int(days * 86400 * 1000), now
    pw = W - ML - MR
    top1 = MT
    top2 = MT + PLOT_H + GAP
    H = top2 + PLOT_H + 70

    def x(ms):
        return ML + pw * (ms - x0_ms) / max(1, x1_ms - x0_ms)

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
         f'<rect width="100%" height="100%" fill="{S["surface"]}"/>',
         f'<text x="{ML}" y="28" font-size="18" font-weight="600" fill="{S["text"]}">WAN health — {_e(network or "UniFi")}</text>',
         f'<text x="{ML}" y="48" font-size="12" fill="{S["text2"]}">Last {days:g} day{"s" if days != 1 else ""} · '
         f'{len(points)} speed test{"s" if len(points) != 1 else ""} · {len(events)} WAN event{"s" if len(events) != 1 else ""}</text>']

    # time ticks (shared)
    n_ticks = 6
    for i in range(n_ticks + 1):
        ms = x0_ms + (x1_ms - x0_ms) * i / n_ticks
        xx = x(ms)
        for top in (top1, top2):
            o.append(f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{top + PLOT_H}" stroke="{S["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{xx:.1f}" y="{top2 + PLOT_H + 18}" font-size="11" fill="{S["muted"]}" text-anchor="middle">{_e(_fmt_time(ms, days))}</text>')

    def panel(top, title, series, unit):
        vals = [p[k] for k, _, _ in series for p in points if p.get(k) is not None]
        ymax = _nice_max(max(vals) * 1.1) if vals else 1
        o.append(f'<text x="{ML}" y="{top - 12}" font-size="13" font-weight="600" fill="{S["text"]}">{_e(title)}</text>')
        for i in range(5):
            v = ymax * i / 4
            yy = top + PLOT_H - PLOT_H * i / 4
            o.append(f'<line x1="{ML}" y1="{yy:.1f}" x2="{W - MR}" y2="{yy:.1f}" stroke="{S["grid"]}" stroke-width="1"/>')
            o.append(f'<text x="{ML - 8}" y="{yy + 4:.1f}" font-size="11" fill="{S["muted"]}" text-anchor="end">{v:g}</text>')
        o.append(f'<text x="{ML - 8}" y="{top - 12}" font-size="11" fill="{S["muted"]}" text-anchor="end">{_e(unit)}</text>')

        def y(v):
            return top + PLOT_H - PLOT_H * v / ymax
        ends = [y(pp[k]) for k, _, _ in series
                for pp in [next((q for q in reversed(points) if q.get(k) is not None), None)] if pp]
        # Direct end labels only when they can't collide; otherwise legend + hover carry the values.
        label_ends = all(abs(a - b) >= 16 for i, a in enumerate(ends) for b in ends[i + 1:])
        for key, color, name in series:
            pts = [(x(p["at"]), y(p[key]), p) for p in points if p.get(key) is not None]
            if len(pts) > 1:
                d = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py, _) in enumerate(pts))
                o.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
            for px, py, p in pts:
                tip = f"{_fmt_full(p['at'])} · {name} {p[key]:g} {unit}" + (" (run by this app)" if p.get("source") == "app" else "")
                o.append(f'<g><title>{_e(tip)}</title><circle cx="{px:.1f}" cy="{py:.1f}" r="10" fill="transparent"/>'
                         f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{color}" stroke="{S["surface"]}" stroke-width="2"/></g>')
            if pts and label_ends:   # direct label at the latest point (text in text ink, the dot carries the color)
                px, py, p = pts[-1]
                o.append(f'<text x="{min(px + 10, W - MR - 4):.1f}" y="{py - 8:.1f}" font-size="11" fill="{S["text2"]}" '
                         f'text-anchor="{"end" if px > W - MR - 90 else "start"}">{p[key]:g}</text>')
        if not vals:
            o.append(f'<text x="{ML + pw / 2:.1f}" y="{top + PLOT_H / 2:.1f}" font-size="13" fill="{S["muted"]}" text-anchor="middle">'
                     f'No speed tests in this period</text>')

    panel(top1, "Throughput", [("download_mbps", S["down"], "download"), ("upload_mbps", S["up"], "upload")], "Mbps")
    panel(top2, "Latency", [("latency_ms", S["lat"], "latency")], "ms")

    # WAN events: a marker spanning both charts
    for ev in events:
        xx = x(ev["at"])
        color = S["critical"] if is_outage(ev) else S["warning"]
        tip = f"{_fmt_full(ev['at'])} · {ev['title']}"
        o.append(f'<g><title>{_e(tip)}</title>'
                 f'<rect x="{xx - 5:.1f}" y="{top1}" width="10" height="{top2 + PLOT_H - top1}" fill="transparent"/>'
                 f'<line x1="{xx:.1f}" y1="{top1}" x2="{xx:.1f}" y2="{top2 + PLOT_H}" stroke="{color}" stroke-width="1.5" opacity="0.8"/>'
                 f'<path d="M{xx - 6:.1f},{top1 - 2} L{xx + 6:.1f},{top1 - 2} L{xx:.1f},{top1 + 8} Z" fill="{color}"/></g>')

    # legend (two series => always a legend; plus the event key)
    ly = H - 24
    items = [(S["down"], "line", "Download"), (S["up"], "line", "Upload"), (S["lat"], "line", "Latency"),
             (S["critical"], "tri", "WAN down / lost"), (S["warning"], "tri", "Other WAN event")]
    lx = ML
    for color, shape, label in items:
        if shape == "line":
            o.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 18}" y2="{ly}" stroke="{color}" stroke-width="2"/>'
                     f'<circle cx="{lx + 9}" cy="{ly}" r="4" fill="{color}" stroke="{S["surface"]}" stroke-width="2"/>')
        else:
            o.append(f'<path d="M{lx + 3},{ly - 5} L{lx + 15},{ly - 5} L{lx + 9},{ly + 5} Z" fill="{color}"/>')
        o.append(f'<text x="{lx + 24}" y="{ly + 4}" font-size="11" fill="{S["text2"]}">{_e(label)}</text>')
        lx += 34 + 7 * len(label) + 16
    o.append("</svg>")
    return "\n".join(o)


# ── scheduler ─────────────────────────────────────────────────────────────────
class Scheduler:
    """Runs a speed test for the ACTIVE connection every N hours (per connection; 0 = off)."""

    def __init__(self, get_profile, get_client, last_run_ms, record):
        self._get_profile, self._get_client = get_profile, get_client
        self._last_run_ms, self._record = last_run_ms, record
        self.last_error = None
        self._stop = threading.Event()

    def due(self, profile, now_ms):
        hours = int(profile.get("speedtest_hours") or 0)
        if not hours:
            return False
        last = self._last_run_ms(profile["id"])
        return last is None or now_ms - last >= hours * 3600 * 1000

    def tick(self, now_ms=None):
        """One check. -> the recorded point, or None."""
        p = self._get_profile()
        if not p or not self.due(p, now_ms or int(time.time() * 1000)) or is_running():
            return None
        # "ran_at" is THIS computer's clock: the schedule must not depend on the console's clock
        # (a console set to the wrong date would otherwise trigger a test every minute).
        ran_at = int(time.time() * 1000)
        try:
            c = self._get_client()
            if c.login_blocked():
                return None
            point = run_speedtest(c)
            self.last_error = None
            return self._record(p["id"], dict(point, ran_at=ran_at), "schedule")
        except UniFiError as e:
            self.last_error = {"at": ran_at, "error": str(e)}
            # don't retry every minute after a failure: count it as a run
            self._record(p["id"], {"at": ran_at, "ran_at": ran_at, "download_mbps": None, "upload_mbps": None,
                                   "latency_ms": None, "source": "app", "error": str(e)}, "schedule")
            return None

    def start(self, interval=60):
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True, name="speedtest-scheduler").start()

    def stop(self):
        self._stop.set()
