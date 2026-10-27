"""
AI usage and estimated cost.

Token counts are exact: every API response reports them (input, output, and the
prompt-cache reads and writes). Dollars are an ESTIMATE: tokens x the published
price per million tokens. Neither Anthropic nor OpenAI has a price API, so prices
come from, in order:
  1. your own override (data/prices.json, "custom"), set in the app
  2. LiteLLM's public price list (github.com/BerriAI/litellm,
     model_prices_and_context_window.json), fetched when you press "Update prices"
     and cached in data/prices.json. It covers both Claude and GPT models.
  3. the table below, copied from Anthropic's pricing page
     (platform.claude.com/docs/en/about-claude/pricing) on 2026-10-27.
Taxes, discounts, batch and priority pricing are not included.
"""

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta

import config_store as cfg
import history

PRICES_PATH = os.path.join(cfg.DATA_DIR, "prices.json")
USAGE = "usage.jsonl"            # in data/, rotated like the change log
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI_PRICING_URL = "https://openai.com/api/pricing/"
BUILTIN_DATE = "2026-10-27"
_lock = threading.Lock()

# $ per million tokens: input, 5-minute cache write, cache read, output.
# (This app caches with the default 5-minute TTL.)
BUILTIN = {
    "claude-fable-5-1":  (10.00, 12.50, 0.25, 50.00),
    "claude-mythos-5-1": (10.00, 12.50, 0.25, 50.00),
    "claude-fable-5":    (10.00, 12.50, 1.00, 50.00),
    "claude-mythos-5":   (10.00, 12.50, 1.00, 50.00),
    "claude-opus-5-5":   (4.00, 5.00, 0.20, 20.00),
    "claude-opus-5":     (5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-8":   (5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-7":   (5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-6":   (5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-5":   (5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-1":   (15.00, 18.75, 1.50, 75.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-sonnet-5":   (2.00, 2.50, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 0.30, 15.00),
    "claude-haiku-5-5":  (0.10, 0.125, 0.01, 0.50),     # prompts up to 100k tokens; see TIERS
    "claude-haiku-4-5":  (1.00, 1.25, 0.10, 5.00),
}
# Long prompts cost more on some models: (threshold in input tokens, prices above it).
TIERS = {"claude-haiku-5-5": (100_000, (0.50, 0.625, 0.05, 2.50))}


def _load():
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    tmp = PRICES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PRICES_PATH)


def _base_id(model):
    """claude-sonnet-5-5-20261001 -> claude-sonnet-5-5 (dated snapshots share the alias's price)."""
    m = str(model or "")
    parts = m.rsplit("-", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8 else m


def price(model, input_tokens=0):
    """-> ({"input", "cache_write", "cache_read", "output"} $/MTok, source) or (None, None)."""
    data = _load()
    mid, base = str(model or ""), _base_id(model)
    for key in (mid, base):
        c = (data.get("custom") or {}).get(key)
        if c:
            return dict(c), "your price"
    tier = TIERS.get(base)
    if tier and input_tokens > tier[0]:
        i, w, r, o = tier[1]
        return {"input": i, "cache_write": w, "cache_read": r, "output": o}, f"Anthropic pricing page ({BUILTIN_DATE}), long-prompt rate"
    for key in (mid, base):
        p = (data.get("litellm") or {}).get(key)
        if p:
            return dict(p), f"LiteLLM price list ({(data.get('litellm_updated') or '')[:10]})"
    b = BUILTIN.get(base)
    if b:
        i, w, r, o = b
        return {"input": i, "cache_write": w, "cache_read": r, "output": o}, f"Anthropic pricing page ({BUILTIN_DATE})"
    return None, None


def cost(model, u):
    """u: {"input", "cache_write", "cache_read", "output"} tokens -> (dollars or None, source)."""
    total_in = u.get("input", 0) + u.get("cache_write", 0) + u.get("cache_read", 0)
    p, src = price(model, total_in)
    if not p:
        return None, None
    d = sum(u.get(k, 0) * (p.get(k) or 0) for k in ("input", "cache_write", "cache_read", "output")) / 1_000_000
    return round(d, 6), src


def update_from_litellm(timeout=30):
    """Fetch LiteLLM's public price list and keep the Claude and GPT entries. -> count."""
    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "unifi-flinchbot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read(20_000_000))
    keep = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or v.get("litellm_provider") not in ("anthropic", "openai"):
            continue
        if not (k.startswith("claude-") or k.startswith("gpt-") or k[:2] in ("o1", "o3", "o4")):
            continue
        try:
            i = float(v.get("input_cost_per_token") or 0) * 1e6
            o = float(v.get("output_cost_per_token") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        if not i and not o:
            continue
        r_ = v.get("cache_read_input_token_cost")
        w = v.get("cache_creation_input_token_cost")
        keep[k] = {"input": round(i, 4), "output": round(o, 4),
                   "cache_read": round(float(r_) * 1e6, 4) if r_ else round(i * 0.1, 4),
                   "cache_write": round(float(w) * 1e6, 4) if w else round(i * 1.25, 4)}
    if not keep:
        raise ValueError("The LiteLLM price list didn't contain any Claude or GPT prices.")
    with _lock:
        data = _load()
        data["litellm"], data["litellm_updated"] = keep, datetime.now().astimezone().isoformat(timespec="seconds")
        _save(data)
    return len(keep)


def set_custom(model, prices):
    """prices None = remove the override."""
    with _lock:
        data = _load()
        custom = data.setdefault("custom", {})
        if prices is None:
            custom.pop(model, None)
        else:
            clean = {}
            for k in ("input", "cache_write", "cache_read", "output"):
                v = float(prices.get(k))
                if not 0 <= v <= 10_000:
                    raise ValueError("Prices are dollars per million tokens, between 0 and 10,000.")
                clean[k] = v
            custom[model] = clean
        _save(data)


def get_budget():
    try:
        return float(_load().get("monthly_budget") or 0)
    except (TypeError, ValueError):
        return 0.0


def set_budget(dollars):
    v = float(dollars or 0)
    if v < 0:
        raise ValueError("The budget can't be negative.")
    with _lock:
        data = _load()
        data["monthly_budget"] = v
        _save(data)


# ── usage log ─────────────────────────────────────────────────────────────────
def record(provider, model, u, kind, chat_id, connection=None):
    d, src = cost(model, u)
    rec = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "provider": provider, "model": model,
           "kind": kind, "chat": chat_id, "connection": connection, **{k: int(u.get(k, 0)) for k in
                                                                       ("input", "cache_write", "cache_read", "output")},
           "cost": d, "price_source": src}
    history.append(USAGE, rec)
    return rec


def _sum(recs):
    out = {"calls": len(recs), "input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost": 0.0, "unpriced": 0}
    for r in recs:
        for k in ("input", "cache_write", "cache_read", "output"):
            out[k] += int(r.get(k) or 0)
        if r.get("cost") is None:
            out["unpriced"] += 1
        else:
            out["cost"] += float(r["cost"])
    out["tokens"] = out["input"] + out["cache_write"] + out["cache_read"] + out["output"]
    out["cost"] = round(out["cost"], 4)
    return out


def summary(chat_id=None):
    recs = history.read(USAGE)
    now = datetime.now().astimezone()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)

    def since(t):
        return [r for r in recs if datetime.fromisoformat(r["at"]) >= t]
    by_model = {}
    for r in since(month0):
        by_model.setdefault(r["model"], []).append(r)
    models = sorted(by_model, key=lambda m: -_sum(by_model[m])["cost"])
    budget = get_budget()
    month = _sum(since(month0))
    return {
        "chat": _sum([r for r in recs if chat_id and r.get("chat") == chat_id]),
        "today": _sum(since(day0)), "last7": _sum(since(now - timedelta(days=7))), "month": month,
        "by_model": [dict(_sum(by_model[m]), model=m, price=price(m)[0], price_source=price(m)[1]) for m in models],
        "budget": budget, "over_budget": bool(budget and month["cost"] >= budget),
        "near_budget": bool(budget and month["cost"] >= 0.8 * budget),
        "prices_updated": _load().get("litellm_updated"),
        "sources": {"anthropic": ANTHROPIC_PRICING_URL, "openai": OPENAI_PRICING_URL, "litellm": LITELLM_URL},
        "builtin_date": BUILTIN_DATE,
    }
