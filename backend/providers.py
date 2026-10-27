"""
AI providers: Anthropic (Claude, Messages API) and OpenAI (ChatGPT models, Responses API).

The agent works with a provider-neutral shape:
  * a Turn: the model's visible text, its tool calls [{id, name, input}], whether
    it hit the output limit, and the native items to append to history
  * tool results: [{id, content, is_error}]
Each provider owns its own history format, so a conversation never mixes the two
(the app clears the chat when you switch provider).

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


class Turn:
    def __init__(self, texts, calls, truncated, items):
        self.texts = texts            # list[str] - visible text from the model
        self.calls = calls            # list[{id, name, input}] - input None if unparseable
        self.truncated = truncated    # hit the output-token limit
        self.items = items            # native history items for this assistant turn


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

    def complete(self, history, system, tools, model, max_tokens):
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=copy.deepcopy(history),
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
        return Turn(texts, calls, resp.stop_reason == "max_tokens", [{"role": "assistant", "content": blocks}])

    @staticmethod
    def drop_calls(items):
        blocks = [b for b in items[0]["content"] if b.get("type") != "tool_use"] or [{"type": "text", "text": "(cut off)"}]
        return [{"role": "assistant", "content": blocks}]

    @staticmethod
    def append_user(history, results=(), text=None):
        """Tool results first, then text; merged into a trailing user turn so roles alternate."""
        blocks = []
        for r in results:
            b = {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            if r.get("is_error"):
                b["is_error"] = True
            blocks.append(b)
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
            raise RuntimeError("The 'openai' package isn't installed. Run start.bat again to install it.")
        self.client = OpenAI(api_key=api_key)

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
        return Turn(texts, calls, truncated, items)

    @staticmethod
    def drop_calls(items):
        # A reasoning item must be followed by the item it produced, so when we
        # drop a cut-off call we drop the reasoning too and keep only messages.
        kept = [i for i in items if i.get("type") == "message"]
        return kept or [{"role": "assistant", "content": "(cut off)"}]

    @staticmethod
    def append_user(history, results=(), text=None):
        for r in results:
            out = r["content"]
            if r.get("is_error") and not str(out).startswith("ERROR"):
                out = "ERROR: " + str(out)
            history.append({"type": "function_call_output", "call_id": r["id"], "output": out})
        if text:
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


PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-6.1-sol"}


def get_provider(name, api_key):
    cls = PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"Unknown AI provider '{name}'")
    return cls(api_key)


def provider_error_message(e, provider_name=None, model=None):
    order = [PROVIDERS[provider_name]] if provider_name in PROVIDERS else []
    order += [p for p in PROVIDERS.values() if p not in order]
    for p in order:
        msg = p.error_message(e, model)
        if msg:
            return msg
    return None
