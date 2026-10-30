"""
AI providers: Anthropic (Claude, Messages API), OpenAI (ChatGPT models, Responses API)
and Local AI (any OpenAI-compatible Chat Completions server: Ollama, LM Studio,
llama.cpp, vLLM, Jan, LocalAI, or a hosted one such as OpenRouter).

The agent works with a provider-neutral shape:
  * a Turn: the model's visible text, its tool calls [{id, name, input}], whether
    it hit the output limit, and the native items for this assistant turn
  * tool results: [{id, content, is_error}]
  * the conversation record (the agent's history), a list of entries:
      {"role": "user", "results": [...], "text": str|None, "images": [...]}
      {"role": "assistant", "texts": [...], "calls": [...], "by": {"provider", "model"},
       "native": the provider's own items for that turn}
    render(record) turns it into the provider's request format. A turn the same
    provider wrote is sent back exactly as it came (with Claude's thinking blocks or
    OpenAI's encrypted reasoning); a turn another AI wrote is rebuilt from its text and
    tool calls. That is what lets a conversation move between AIs (second opinion,
    switching provider in Settings).

OpenAI notes (Oct 2026): tool calling on the newest GPT models requires the
Responses API. We call it statelessly (store=False) so OpenAI doesn't keep the
conversation, and request reasoning.encrypted_content so the model's reasoning
can be passed back between tool rounds, as OpenAI documents for store=False.
"""

import copy
import json
import re
from datetime import datetime, timezone

from anthropic import Anthropic

try:
    from openai import OpenAI
except ImportError:          # the app still works with Claude only
    OpenAI = None


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)      # reasoning some local models print inline


def _safe_id(v):
    """Tool-call ids cross providers (second opinion): keep them to what all of them accept."""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(v or ""))[:64]
    return s or "call"


def _result_text(r):
    c = r.get("content")
    return c if isinstance(c, str) else json.dumps(c, default=str)


class Turn:
    def __init__(self, texts, calls, truncated, items, usage=None):
        self.texts = texts            # list[str] - visible text from the model
        self.calls = calls            # list[{id, name, input}] - input None if unparseable
        self.truncated = truncated    # hit the output-token limit
        self.items = items            # native history items for this assistant turn
        # tokens billed for this call: {"input", "cache_write", "cache_read", "output"}
        self.usage = usage or {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}


def _n(obj, name):
    v = getattr(obj, name, None) if obj is not None else None
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


# ── Anthropic ─────────────────────────────────────────────────────────────────
class AnthropicProvider:
    name, label, vendor = "anthropic", "Claude", "Anthropic"
    key_prefix, key_url = "sk-ant-", "https://console.anthropic.com/settings/keys"

    def __init__(self, api_key):
        self.client = Anthropic(api_key=api_key)

    def abort(self):
        """Close the client, which makes a request waiting on another thread give up (Stop button)."""
        try:
            self.client.close()
        except Exception:
            pass

    def complete(self, history, system, tools, model, max_tokens):
        # cache_control (top-level): the tool schemas, system prompt and earlier turns are
        # identical from one round of the tool loop to the next, so they are served from
        # the prompt cache instead of being re-billed in full. The agent keeps the system
        # prompt free of anything that changes minute to minute for the same reason.
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=copy.deepcopy(history),
            cache_control={"type": "ephemeral"},
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                   for t in tools])
        blocks = []
        for b in resp.content:
            t = getattr(b, "type", None)
            if t == "text":
                blocks.append({"type": "text", "text": b.text})
            elif t == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            else:                                   # thinking etc. go back untouched
                blocks.append(b.model_dump(exclude_none=True))
        blocks = blocks or [{"type": "text", "text": "(no response)"}]
        texts = [b["text"] for b in blocks if b.get("type") == "text" and b.get("text", "").strip()]
        calls = [{"id": b["id"], "name": b["name"], "input": b["input"]} for b in blocks if b.get("type") == "tool_use"]
        u = getattr(resp, "usage", None)
        usage = {"input": _n(u, "input_tokens"), "cache_write": _n(u, "cache_creation_input_tokens"),
                 "cache_read": _n(u, "cache_read_input_tokens"), "output": _n(u, "output_tokens")}
        return Turn(texts, calls, resp.stop_reason == "max_tokens", [{"role": "assistant", "content": blocks}], usage)

    @staticmethod
    def drop_calls(items):
        blocks = [b for b in items[0]["content"] if b.get("type") != "tool_use"] or [{"type": "text", "text": "(cut off)"}]
        return [{"role": "assistant", "content": blocks}]

    def render(self, record):
        out = []
        for e in record:
            if e["role"] == "user":
                out.append({"role": "user", "content": self._user_blocks(e.get("results") or (), e.get("text"),
                                                                         e.get("images") or ())})
            elif e.get("native") is not None and (e.get("by") or {}).get("provider") == self.name:
                out.extend(copy.deepcopy(e["native"]))
            else:
                blocks = [{"type": "text", "text": t} for t in e.get("texts") or () if str(t).strip()]
                blocks += [{"type": "tool_use", "id": _safe_id(c["id"]), "name": c["name"],
                            "input": c["input"] if isinstance(c.get("input"), dict) else {}}
                           for c in e.get("calls") or ()]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no response)"}]})
        return out

    @staticmethod
    def _user_blocks(results, text, images):
        blocks = []
        for r in results:
            b = {"type": "tool_result", "tool_use_id": _safe_id(r["id"]), "content": r["content"]}
            if r.get("is_error"):
                b["is_error"] = True
            blocks.append(b)
        for im in images or ():
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}})
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    @staticmethod
    def append_user(history, results=(), text=None, images=()):
        """Tool results first, then images, then text; merged into a trailing user turn so roles alternate."""
        blocks = []
        for r in results:
            b = {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            if r.get("is_error"):
                b["is_error"] = True
            blocks.append(b)
        for im in images or ():
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}})
        if text:
            blocks.append({"type": "text", "text": text})
        if history and history[-1]["role"] == "user":
            prev = history[-1]["content"]
            prev = [{"type": "text", "text": prev}] if isinstance(prev, str) else list(prev)
            allb = prev + blocks
            history[-1] = {"role": "user", "content": [b for b in allb if b.get("type") == "tool_result"] +
                                                      [b for b in allb if b.get("type") != "tool_result"]}
        else:
            history.append({"role": "user", "content": blocks})

    @staticmethod
    def is_boundary(item):
        """A user message with no tool results: safe place to cut old history."""
        if item.get("role") != "user":
            return False
        c = item["content"]
        return isinstance(c, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)

    def list_models(self):
        out = []
        for m in self.client.models.list(limit=100):            # iterating auto-paginates
            retires = getattr(m, "retires_at", None)
            out.append({
                "id": m.id, "name": getattr(m, "display_name", None) or m.id,
                "line": getattr(m, "line", None),
                "lifecycle": getattr(m, "lifecycle", None) or "active",
                "retires_at": _iso(retires)[:10] if retires else None,
                "created_at": _iso(getattr(m, "created_at", None)),
            })
        return out

    @staticmethod
    def error_message(e, model):
        import anthropic
        if isinstance(e, anthropic.AuthenticationError):
            return "Anthropic rejected the API key. Open Settings and paste a valid key."
        if isinstance(e, anthropic.PermissionDeniedError):
            return "This Anthropic key isn't allowed to do that (check the key's workspace/permissions)."
        if isinstance(e, anthropic.NotFoundError):
            return f"Model '{model}' wasn't found for this key. Pick another model in Settings."
        if isinstance(e, anthropic.RateLimitError):
            return "Anthropic rate limit reached. Wait a minute and try again."
        if isinstance(e, anthropic.APIConnectionError):
            return "Can't reach api.anthropic.com. Check this PC's internet connection."
        if isinstance(e, anthropic.APIStatusError):
            msg = getattr(e, "message", str(e))
            if "credit balance" in str(msg).lower():
                return "Your Anthropic account is out of credits. Add credits at console.anthropic.com."
            return f"Anthropic API error {e.status_code}: {msg}"
        return None


# ── OpenAI ────────────────────────────────────────────────────────────────────
# OpenAI's model list has no "is this a chat model" flag, so filter by name.
_OPENAI_CHAT = re.compile(r"^(gpt-\d|o\d)")
_OPENAI_NOT_CHAT = re.compile(
    r"(image|audio|realtime|transcribe|tts|embedding|moderation|search|whisper|dall-e|"
    r"live|instruct|translate|cyber|daybreak)", re.I)


def _openai_line(model_id):
    m = re.match(r"^(gpt-\d+(?:\.\d+)?|o\d+)", model_id)
    return m.group(1).upper() if m else "Other"


class OpenAIProvider:
    name, label, vendor = "openai", "ChatGPT", "OpenAI"
    key_prefix, key_url = "sk-", "https://platform.openai.com/api-keys"

    def __init__(self, api_key):
        if OpenAI is None:
            raise RuntimeError("The 'openai' package isn't installed. Close the app and start it again "
                               "(start.bat on Windows, start.command on a Mac) to install it.")
        self.client = OpenAI(api_key=api_key)

    def abort(self):
        """Close the client, which makes a request waiting on another thread give up (Stop button)."""
        try:
            self.client.close()
        except Exception:
            pass

    def complete(self, history, system, tools, model, max_tokens):
        resp = self.client.responses.create(
            model=model, instructions=system, input=copy.deepcopy(history),
            tools=[{"type": "function", "name": t["name"], "description": t["description"],
                    "parameters": t["input_schema"], "strict": False} for t in tools],
            max_output_tokens=max_tokens, store=False, include=["reasoning.encrypted_content"])
        items = [o.model_dump(mode="json", exclude_none=True, by_alias=True) for o in resp.output]
        texts, calls = [], []
        for it in items:
            if it.get("type") == "message":
                for part in it.get("content") or []:
                    if part.get("type") in ("output_text", "refusal"):
                        t = part.get("text") or part.get("refusal") or ""
                        if t.strip():
                            texts.append(t)
            elif it.get("type") == "function_call":
                try:
                    args = json.loads(it.get("arguments") or "{}")
                except ValueError:
                    args = None
                calls.append({"id": it["call_id"], "name": it["name"], "input": args})
        details = getattr(resp, "incomplete_details", None)
        truncated = getattr(resp, "status", None) == "incomplete" and \
            getattr(details, "reason", None) == "max_output_tokens"
        if not items:
            items = [{"role": "assistant", "content": "(no response)"}]
        u = getattr(resp, "usage", None)
        cached = _n(getattr(u, "input_tokens_details", None), "cached_tokens")
        usage = {"input": max(0, _n(u, "input_tokens") - cached), "cache_write": 0, "cache_read": cached,
                 "output": _n(u, "output_tokens")}          # OpenAI's input_tokens include the cached ones
        return Turn(texts, calls, truncated, items, usage)

    @staticmethod
    def drop_calls(items):
        # A reasoning item must be followed by the item it produced, so when we
        # drop a cut-off call we drop the reasoning too and keep only messages.
        kept = [i for i in items if i.get("type") == "message"]
        return kept or [{"role": "assistant", "content": "(cut off)"}]

    def render(self, record):
        out = []
        for e in record:
            if e["role"] == "user":
                self.append_user(out, [dict(r, id=_safe_id(r["id"])) for r in e.get("results") or ()],
                                 e.get("text"), e.get("images") or ())
            elif e.get("native") is not None and (e.get("by") or {}).get("provider") == self.name:
                out.extend(copy.deepcopy(e["native"]))
            else:
                text = "\n\n".join(t for t in e.get("texts") or () if str(t).strip())
                if text or not e.get("calls"):
                    out.append({"role": "assistant", "content": text or "(no response)"})
                for c in e.get("calls") or ():
                    out.append({"type": "function_call", "call_id": _safe_id(c["id"]), "name": c["name"],
                                "arguments": json.dumps(c["input"] if isinstance(c.get("input"), dict) else {})})
        return out

    @staticmethod
    def append_user(history, results=(), text=None, images=()):
        for r in results:
            out = r["content"]
            if r.get("is_error") and not str(out).startswith("ERROR"):
                out = "ERROR: " + str(out)
            history.append({"type": "function_call_output", "call_id": r["id"], "output": out})
        if images:
            parts = [{"type": "input_image", "detail": "auto",
                      "image_url": f"data:{im['media_type']};base64,{im['data']}"} for im in images]
            history.append({"role": "user", "content": parts + [{"type": "input_text", "text": text or ""}]})
        elif text:
            history.append({"role": "user", "content": text})

    @staticmethod
    def is_boundary(item):
        return item.get("role") == "user"

    def list_models(self):
        out = []
        for m in self.client.models.list():
            mid = m.id
            if not _OPENAI_CHAT.match(mid) or _OPENAI_NOT_CHAT.search(mid):
                continue
            shutdown = getattr(m, "shutdown_date", None)
            out.append({"id": mid, "name": mid, "line": _openai_line(mid),
                        "lifecycle": "deprecated" if shutdown else "active",
                        "retires_at": _iso(shutdown)[:10] if shutdown else None,
                        "created_at": _iso(getattr(m, "created", None))})
        out.sort(key=lambda x: x["created_at"] or "", reverse=True)       # newest first
        return out

    @staticmethod
    def error_message(e, model):
        try:
            import openai
        except ImportError:
            return None
        if isinstance(e, openai.AuthenticationError):
            return "OpenAI rejected the API key. Open Settings and paste a valid key."
        if isinstance(e, openai.PermissionDeniedError):
            return "This OpenAI key isn't allowed to do that (check the project's model permissions)."
        if isinstance(e, openai.NotFoundError):
            return f"Model '{model}' wasn't found for this OpenAI key. Pick another model in Settings."
        if isinstance(e, openai.RateLimitError):
            if "insufficient_quota" in str(e) or "quota" in str(getattr(e, "message", "")).lower():
                return ("Your OpenAI account has no API credit. Add credit at platform.openai.com "
                        "(a ChatGPT Plus/Pro subscription doesn't include API usage).")
            return "OpenAI rate limit reached. Wait a minute and try again."
        if isinstance(e, openai.APIConnectionError):
            return "Can't reach api.openai.com. Check this PC's internet connection."
        if isinstance(e, openai.APIStatusError):
            return f"OpenAI API error {e.status_code}: {getattr(e, 'message', e)}"
        return None


# ── Local AI (OpenAI-compatible Chat Completions) ─────────────────────────────
def normalize_base_url(url):
    """'office-pc:11434' -> 'http://office-pc:11434/v1'. A path other than / is kept as typed."""
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    m = re.match(r"^(https?://[^/\s]+)(/\S*)?$", u, re.I)
    if not m:
        raise ValueError("That isn't a web address. Use something like http://office-pc:11434")
    return u if m.group(2) else u + "/v1"


class LocalProvider:
    """Any server that speaks OpenAI's Chat Completions API, which is what local AI servers
    offer: Ollama (port 11434), LM Studio (1234), llama.cpp's llama-server (8080), vLLM
    (8000), Jan (1337), LocalAI; also hosted ones like OpenRouter or a LiteLLM proxy.
    Nothing provider-specific is kept between turns, so render() rebuilds every turn."""
    name, label, vendor = "local", "Local AI", "your local AI server"
    key_prefix, key_url = "", ""

    def __init__(self, api_key, base_url=None):
        if OpenAI is None:
            raise RuntimeError("The 'openai' package isn't installed. Close the app and start it again "
                               "(start.bat on Windows, start.command on a Mac) to install it.")
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise ValueError("No address for the local AI yet. Open Settings and choose Local AI.")
        # Most local servers need no key, but the SDK insists on one. Local models can be slow
        # (loading into memory, long answers on a CPU), hence the generous timeout.
        self.client = OpenAI(api_key=api_key or "local", base_url=self.base_url, timeout=900, max_retries=1)

    def abort(self):
        try:
            self.client.close()
        except Exception:
            pass

    def render(self, record):
        msgs = []
        for e in record:
            if e["role"] == "user":
                self.append_user(msgs, [dict(r, id=_safe_id(r["id"])) for r in e.get("results") or ()],
                                 e.get("text"), e.get("images") or ())
            else:
                m = {"role": "assistant", "content": "\n\n".join(t for t in e.get("texts") or () if str(t).strip())}
                if e.get("calls"):
                    m["tool_calls"] = [{"id": _safe_id(c["id"]), "type": "function", "function": {
                        "name": c["name"], "arguments": json.dumps(c["input"] if isinstance(c.get("input"), dict) else {})}}
                        for c in e["calls"]]
                msgs.append(m)
        return msgs

    def complete(self, history, system, tools, model, max_tokens):
        kw = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system}] + copy.deepcopy(history)}
        if tools:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["input_schema"]}} for t in tools]
        resp = self.client.chat.completions.create(**kw)
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        msg = getattr(choice, "message", None)
        text = _THINK.sub("", getattr(msg, "content", None) or "").strip()
        calls = []
        for i, tc in enumerate(getattr(msg, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            try:
                args = json.loads(getattr(fn, "arguments", None) or "{}")
                args = args if isinstance(args, dict) else None
            except (TypeError, ValueError):
                args = None
            calls.append({"id": getattr(tc, "id", None) or f"call_{i}", "name": getattr(fn, "name", "") or "",
                          "input": args})
        item = {"role": "assistant", "content": text}
        if calls:
            item["tool_calls"] = [{"id": _safe_id(c["id"]), "type": "function", "function": {
                "name": c["name"], "arguments": json.dumps(c["input"] or {})}} for c in calls]
        u = getattr(resp, "usage", None)
        usage = {"input": _n(u, "prompt_tokens"), "cache_write": 0, "cache_read": 0, "output": _n(u, "completion_tokens")}
        truncated = getattr(choice, "finish_reason", None) == "length"
        return Turn([text] if text else [], calls, truncated, [item], usage)

    @staticmethod
    def drop_calls(items):
        return [{"role": "assistant", "content": (items[0].get("content") if items else "") or "(cut off)"}]

    @staticmethod
    def append_user(history, results=(), text=None, images=()):
        for r in results:
            out = _result_text(r)
            if r.get("is_error") and not out.startswith("ERROR"):
                out = "ERROR: " + out
            history.append({"role": "tool", "tool_call_id": r["id"], "content": out})
        if images:
            parts = [{"type": "image_url", "image_url": {"url": f"data:{im['media_type']};base64,{im['data']}"}}
                     for im in images]
            history.append({"role": "user", "content": parts + [{"type": "text", "text": text or ""}]})
        elif text:
            history.append({"role": "user", "content": text})

    @staticmethod
    def is_boundary(item):
        return item.get("role") == "user"

    def list_models(self):
        out = [{"id": m.id, "name": m.id, "line": None, "lifecycle": "active", "retires_at": None,
                "created_at": _iso(getattr(m, "created", None))} for m in self.client.models.list()]
        return sorted(out, key=lambda m: m["id"].lower())

    @staticmethod
    def error_message(e, model):
        import openai
        url = str(getattr(getattr(e, "request", None), "url", "") or "")
        where = re.sub(r"/v1/.*$", "", url) or "the address in Settings"
        if isinstance(e, openai.APITimeoutError):
            return (f"The local AI at {where} took too long to answer. A big model on a slow computer can take "
                    f"minutes; try a smaller model, or ask a narrower question.")
        if isinstance(e, openai.APIConnectionError):
            return (f"Can't reach the local AI at {where}. Check that the server is running and accepts "
                    f"connections from this computer (Ollama: set OLLAMA_HOST=0.0.0.0 on that computer; "
                    f"LM Studio: turn on 'Serve on local network'), and that a firewall isn't blocking the port.")
        if isinstance(e, openai.AuthenticationError):
            return "The local AI server rejected the key. Check the key in Settings (most local servers need none)."
        if isinstance(e, openai.NotFoundError):
            return (f"The local AI server has no model '{model}'. Pick one from the list in Settings "
                    f"(Ollama: run 'ollama pull {model}' on that computer).")
        if isinstance(e, openai.APIStatusError):
            msg = str(getattr(e, "message", "") or e)
            low = msg.lower()
            if "context" in low or "too long" in low or "exceed" in low:
                return ("The conversation no longer fits in the local model's context. Clear the chat, or raise the "
                        "context size on the server and in Settings.")
            if "image" in low or "vision" in low or "multimodal" in low:
                return ("This local model can't read images. Pick a vision model (for example qwen2.5vl or gemma3) "
                        "or remove the image.")
            if "tool" in low or "function" in low:
                return ("This local model doesn't support tools (function calling), which the app needs. "
                        "Pick a model marked with tool support, ideally 20B parameters or larger.")
            return f"Local AI error {e.status_code}: {msg}"
        return None


PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "local": LocalProvider}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-6.1-sol", "local": ""}


def get_provider(name, api_key, base_url=None):
    cls = PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"Unknown AI provider '{name}'")
    return cls(api_key, base_url) if cls is LocalProvider else cls(api_key)


def provider_error_message(e, provider_name=None, model=None):
    order = [PROVIDERS[provider_name]] if provider_name in PROVIDERS else []
    order += [p for p in PROVIDERS.values() if p not in order]
    for p in order:
        msg = p.error_message(e, model)
        if msg:
            return msg
    return None
