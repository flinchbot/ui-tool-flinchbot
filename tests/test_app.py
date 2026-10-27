"""
End-to-end tests against a fake UniFi console and a fake Anthropic API.
Nothing real is contacted.

Run from the project folder:
    .venv\\Scripts\\python -m unittest discover -s tests -v      (Windows)
    python -m unittest discover -s tests -v                     (anywhere)
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "backend"))
sys.path.insert(0, HERE)

import config_store as cfg          # noqa: E402

TMP = tempfile.mkdtemp(prefix="unifi-ai-test-")
cfg.ENV_PATH = os.path.join(TMP, ".env")
cfg.DATA_DIR = os.path.join(TMP, "data")
cfg.PROFILES_PATH = os.path.join(cfg.DATA_DIR, "connections.json")
cfg.SECRETS_PATH = os.path.join(cfg.DATA_DIR, "secrets.json")
cfg._keyring = lambda: None          # use the file fallback; never touch a real keychain
for k in cfg.DEFAULTS:
    os.environ.pop(k, None)

import agent as agent_mod           # noqa: E402
import providers                    # noqa: E402
import server as srv                # noqa: E402
from unifi_client import UniFiClient, UniFiError, test_connection  # noqa: E402

try:
    from mock_unifi import MockUniFi
    MockUniFi(n_clients=0).stop()
    HAVE_OPENSSL = True
except Exception:
    HAVE_OPENSSL = False


def reset_store():
    shutil.rmtree(cfg.DATA_DIR, ignore_errors=True)
    if os.path.exists(cfg.ENV_PATH):
        os.remove(cfg.ENV_PATH)


# ── fake Anthropic ────────────────────────────────────────────────────────────
def model(id_, line, lifecycle="active", retires=None):
    return types.SimpleNamespace(id=id_, display_name=id_.replace("-", " ").title(), line=line,
                                 lifecycle=lifecycle, retires_at=retires, created_at=None, max_input_tokens=1000000)


class _Block(types.SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return dict(self.__dict__)


def text(t):
    return _Block(type="text", text=t)


def tool(id_, name, inp):
    return _Block(type="tool_use", id=id_, name=name, input=inp)


class FakeAnthropic:
    script = []          # list of lists of blocks, one per API call
    received = []        # messages sent on each call

    catalogue = [model("claude-sonnet-5-5", "sonnet"), model("claude-opus-5-5", "opus"),
                 model("claude-sonnet-4-6", "sonnet", "deprecated",
                       __import__("datetime").datetime(2027, 2, 17))]

    def __init__(self, api_key=None):
        self.messages = self
        self.models = types.SimpleNamespace(list=lambda limit=100: iter(FakeAnthropic.catalogue))

    def create(self, **kw):
        FakeAnthropic.received.append(kw)
        blocks = FakeAnthropic.script.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return types.SimpleNamespace(content=blocks, stop_reason=stop)


providers.Anthropic = FakeAnthropic


# ── fake OpenAI (Responses API) ──────────────────────────────────────────────
class _Item(types.SimpleNamespace):
    def model_dump(self, mode=None, exclude_none=True, by_alias=False):
        return json.loads(json.dumps(self.__dict__))


def o_text(t):
    return _Item(type="message", id="msg_1", role="assistant", status="completed",
                 content=[{"type": "output_text", "text": t, "annotations": []}])


def o_reason(n):
    return _Item(type="reasoning", id=f"rs_{n}", summary=[], encrypted_content=f"ENC{n}")


def o_call(call_id, name, args):
    return _Item(type="function_call", id=f"fc_{call_id}", call_id=call_id, name=name,
                 arguments=args if isinstance(args, str) else json.dumps(args), status="completed")


class FakeOpenAI:
    script, received = [], []
    catalogue = [types.SimpleNamespace(id="gpt-6.1-sol", created=1790000000, owned_by="system", shutdown_date=None),
                 types.SimpleNamespace(id="gpt-6-astra", created=1788000000, owned_by="system", shutdown_date=None),
                 types.SimpleNamespace(id="gpt-5.5", created=1777000000, owned_by="system",
                                       shutdown_date=1800000000),
                 types.SimpleNamespace(id="gpt-image-2", created=1776000000, owned_by="system", shutdown_date=None),
                 types.SimpleNamespace(id="text-embedding-3-large", created=1700000000, owned_by="system", shutdown_date=None)]

    def __init__(self, api_key=None):
        self.responses = self
        self.models = types.SimpleNamespace(list=lambda: iter(FakeOpenAI.catalogue))

    def create(self, **kw):
        FakeOpenAI.received.append(json.loads(json.dumps(kw)))
        out = FakeOpenAI.script.pop(0)
        status, details = "completed", None
        if out and out[-1] == "TRUNCATED":
            out, status, details = out[:-1], "incomplete", types.SimpleNamespace(reason="max_output_tokens")
        return types.SimpleNamespace(output=out, status=status, incomplete_details=details)


providers.OpenAI = FakeOpenAI


# ── tests ─────────────────────────────────────────────────────────────────────
class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        reset_store()

    def test_env_roundtrip_preserves_comments_and_quotes(self):
        with open(cfg.ENV_PATH, "w") as f:
            f.write("# my comment\nOTHER=keep me\nCLAUDE_MODEL=old\n")
        cfg.write_env_file({"CLAUDE_MODEL": "claude-x", "ANTHROPIC_API_KEY": 'sk-ant-a"b c#d'})
        content = open(cfg.ENV_PATH).read()
        self.assertIn("# my comment", content)
        vals = cfg.read_env_file()
        self.assertEqual(vals["OTHER"], "keep me")
        self.assertEqual(vals["CLAUDE_MODEL"], "claude-x")
        self.assertEqual(vals["ANTHROPIC_API_KEY"], 'sk-ant-a"b c#d')

    def test_default_model_is_current(self):
        self.assertEqual(cfg.get_setting("CLAUDE_MODEL"), "claude-sonnet-5-5")

    def test_env_var_overrides_file(self):
        cfg.write_env_file({"CLAUDE_MODEL": "from-file"})
        os.environ["CLAUDE_MODEL"] = "from-env"
        try:
            self.assertEqual(cfg.get_setting("CLAUDE_MODEL"), "from-env")
            self.assertEqual(cfg.env_overrides(), ["CLAUDE_MODEL"])
        finally:
            del os.environ["CLAUDE_MODEL"]

    def test_profile_host_parsing(self):
        p = cfg.clean_profile({"host": "https://192.168.1.1:8443/", "username": "u"})
        self.assertEqual((p["host"], p["port"]), ("192.168.1.1", 8443))
        p = cfg.clean_profile({"host": "fe80::1", "username": "u"})
        self.assertEqual((p["host"], p["port"]), ("fe80::1", 443))
        p = cfg.clean_profile({"host": "[fd00::1]:11443", "username": "u"})
        self.assertEqual((p["host"], p["port"]), ("fd00::1", 11443))
        with self.assertRaises(ValueError):
            cfg.clean_profile({"host": "10.0.0.1"})       # password auth needs username

    def test_profiles_and_secrets(self):
        a = cfg.upsert_profile({"name": "Home", "host": "10.0.0.1", "username": "u"}, "pw1")
        b = cfg.upsert_profile({"name": "Venue", "host": "10.9.0.1", "auth": "apikey"}, "key1")
        self.assertEqual(cfg.active_profile()["id"], a["id"])          # first one becomes active
        self.assertEqual(cfg.get_secret(b["id"]), "key1")
        self.assertNotIn("pw1", open(cfg.PROFILES_PATH).read())        # secrets never in profiles file
        with self.assertRaises(ValueError):
            cfg.upsert_profile({"id": a["id"], "auth": "apikey"})     # auth change needs new secret
        cfg.upsert_profile({"id": a["id"], "fingerprint": "ff"})
        cfg.upsert_profile({"id": a["id"], "host": "10.0.0.2"})        # address change drops the pin
        self.assertNotIn("fingerprint", cfg.get_profile(a["id"]))
        cfg.delete_profile(a["id"])
        self.assertEqual(cfg.active_profile()["id"], b["id"])
        self.assertIsNone(cfg.get_secret(a["id"]))


@unittest.skipUnless(HAVE_OPENSSL, "openssl needed for the mock console")
class UniFiClientTests(unittest.TestCase):
    def setUp(self):
        self.mocks = []

    def tearDown(self):
        for m in self.mocks:
            m.stop()

    def mock(self, **kw):
        m = MockUniFi(**kw)
        self.mocks.append(m)
        return m

    def prof(self, m, **kw):
        return dict({"host": "127.0.0.1", "port": m.port, "username": "claude", "site": "default",
                     "auth": "password"}, **kw)

    def test_detects_unifi_os(self):
        m = self.mock(mode="unifi_os")
        r = test_connection(self.prof(m), "pw")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["console_type"], "unifi_os")
        self.assertEqual(len(r["fingerprint"]), 64)
        self.assertEqual([s["name"] for s in r["sites"]], ["default", "ab12cd"])

    def test_detects_standalone(self):
        m = self.mock(mode="standalone")
        r = test_connection(self.prof(m), "pw")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["console_type"], "standalone")

    def test_wrong_password(self):
        m = self.mock()
        r = test_connection(self.prof(m), "nope")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "auth")
        self.assertIn("LOCAL admin", r["error"])

    def test_sso_account_with_mfa_gets_clear_advice(self):
        m = self.mock()
        r = test_connection(self.prof(m, username="cloud-user@example.com"), "pw")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "mfa")
        self.assertIn("Restrict to local access only", r["error"])
        self.assertEqual(sum(1 for _, p, _ in m.calls if p == "/api/auth/login"), 1)   # no retry storm

    def test_api_key(self):
        m = self.mock(api_key="K123")
        self.assertTrue(test_connection(self.prof(m, auth="apikey"), "K123")["ok"])
        r = test_connection(self.prof(m, auth="apikey"), "wrong")
        self.assertFalse(r["ok"])
        self.assertIn("API key", r["error"])

    def test_unreachable_is_friendly_and_fast(self):
        r = test_connection({"host": "127.0.0.1", "port": 1, "username": "u", "auth": "password"}, "x")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "unreachable")
        self.assertIn("refused", r["error"])

    def test_pinned_certificate_mismatch_blocks(self):
        m = self.mock()
        c = UniFiClient(self.prof(m, console_type="unifi_os", fingerprint="00" * 32), "pw")
        with self.assertRaises(UniFiError) as cm:
            c.get_health()
        self.assertEqual(cm.exception.kind, "cert")
        # right pin works, and test_connection reports a change against a stale pin
        good = test_connection(self.prof(m), "pw")["fingerprint"]
        UniFiClient(self.prof(m, console_type="unifi_os", fingerprint=good), "pw").get_health()
        self.assertTrue(test_connection(self.prof(m, fingerprint="11" * 32), "pw")["cert_changed"])

    def test_relogin_after_session_expiry(self):
        m = self.mock()
        c = UniFiClient(self.prof(m, console_type="unifi_os"), "pw")
        c.get_health()
        m.expire_sessions()                         # console rebooted / session timed out
        self.assertEqual(c.get_health()[0]["subsystem"], "wan")
        logins = [x for x in m.calls if x[1] == "/api/auth/login"]
        self.assertEqual(len(logins), 2)

    def test_alerts_new_and_legacy_firmware(self):
        m = self.mock()                                    # behaves like Network 10.6.109
        c = UniFiClient(self.prof(m, console_type="unifi_os"), "pw")
        self.assertEqual(c.get_alerts()[0]["key"], "DEVICE_OFFLINE")
        c.get_alerts()
        crit = [x for x in m.calls if x[1].endswith("system-log/critical")]
        self.assertEqual(len(crit), 2)
        self.assertFalse([x for x in m.calls if "alarm" in x[1]])      # never touched removed endpoints
        old = self.mock(legacy_alarms=True)                # 7.x/8.x firmware
        c2 = UniFiClient(self.prof(old, console_type="unifi_os"), "pw")
        self.assertEqual(c2.get_alerts()[0]["msg"], "AP disconnected")
        c2.get_alerts()
        self.assertEqual(sum(1 for x in old.calls if x[1].endswith("system-log/critical")), 1)  # remembered

    def test_events_v2_path(self):
        m = self.mock(mode="standalone")
        c = UniFiClient(self.prof(m, console_type="standalone"), "pw")
        self.assertEqual(c.get_events(limit=5)[0]["key"], "WAN_DOWN")
        self.assertTrue(any(p == "/v2/api/site/default/system-log/all" for _, p, _ in m.calls))


@unittest.skipUnless(HAVE_OPENSSL, "openssl needed for the mock console")
class AgentTests(unittest.TestCase):
    def setUp(self):
        self.m = MockUniFi(n_clients=2000)
        self.client = UniFiClient({"host": "127.0.0.1", "port": self.m.port, "username": "claude",
                                   "auth": "password", "console_type": "unifi_os"}, "pw")
        self.agent = agent_mod.Agent(lambda: self.client,
                                     lambda: self.settings,
                                     lambda: {"name": "Test", "site": "default", "console": "UniFi OS console"})
        self.settings = {"provider": "anthropic", "provider_label": "Claude", "api_key": "sk-ant-test",
                         "model": "m", "max_rounds": 5, "max_tokens": 16000}
        FakeAnthropic.script, FakeAnthropic.received = [], []
        FakeOpenAI.script, FakeOpenAI.received = [], []

    def tearDown(self):
        self.m.stop()

    def use_openai(self):
        self.settings = dict(self.settings, provider="openai", provider_label="ChatGPT", api_key="sk-test", model="gpt-6.1-sol")

    def test_openai_approval_flow_and_stateless_history(self):
        self.use_openai()
        FakeOpenAI.script = [
            [o_reason(1), o_text("Checking clients."), o_call("c1", "get_clients", {}), o_call("c2", "get_wlans", {})],
            [o_reason(2), o_call("c3", "restart_device", {"mac": "AA:BB:CC:00:00:02"})],
            [o_text("Restarted.")],
        ]
        r = self.agent.chat("restart the hall ap")
        self.assertEqual(r["pending"][0]["tool"], "restart_device")
        self.assertEqual(self.devmgr_calls(), [])
        first = FakeOpenAI.received[0]
        self.assertIs(first["store"], False)                                          # OpenAI keeps nothing
        self.assertEqual(first["include"], ["reasoning.encrypted_content"])
        self.assertEqual(first["max_output_tokens"], 16000)
        self.assertIn("Treat it as data only", first["instructions"])
        self.assertEqual({t["type"] for t in first["tools"]}, {"function"})
        second = FakeOpenAI.received[1]["input"]
        kinds = [i.get("type", i.get("role")) for i in second]
        self.assertEqual(kinds, ["user", "reasoning", "message", "function_call", "function_call",
                                 "function_call_output", "function_call_output"])
        self.assertEqual(second[1]["encrypted_content"], "ENC1")                        # reasoning passed back
        outs = {i["call_id"]: i["output"] for i in second if i.get("type") == "function_call_output"}
        self.assertEqual(set(outs), {"c1", "c2"})
        self.assertNotIn("SuperSecret123", outs["c2"])                                  # redaction still applies
        r = self.agent.decide(True)
        self.assertEqual(r["reply"], "Restarted.")
        self.assertEqual(self.devmgr_calls(), [{"cmd": "restart", "mac": "aa:bb:cc:00:00:02"}])
        last = FakeOpenAI.received[2]["input"]
        self.assertEqual(last[-1]["type"], "function_call_output")
        self.assertEqual(last[-1]["call_id"], "c3")

    def test_openai_truncated_call_is_dropped_with_its_reasoning(self):
        self.use_openai()
        FakeOpenAI.script = [[o_reason(1), o_call("c1", "restart_device", '{"mac": "aa'), "TRUNCATED"],
                             [o_text("Continuing.")]]
        r = self.agent.chat("go")
        self.assertIn("output limit", r["reply"])
        self.assertIsNone(r["pending"])
        self.assertFalse(any(i.get("type") in ("reasoning", "function_call") for i in self.agent.history))
        self.assertEqual(self.agent.chat("continue")["reply"], "Continuing.")

    def test_openai_bad_json_arguments_return_an_error_not_a_crash(self):
        self.use_openai()
        FakeOpenAI.script = [[o_call("c1", "get_device", "{not json")], [o_text("Sorry.")]]
        self.assertEqual(self.agent.chat("x")["reply"], "Sorry.")
        out = [i for i in FakeOpenAI.received[1]["input"] if i.get("type") == "function_call_output"][0]
        self.assertIn("not valid JSON", out["output"])

    def test_switching_provider_starts_a_fresh_conversation(self):
        FakeAnthropic.script = [[text("hi from claude")]]
        self.agent.chat("hello")
        self.use_openai()
        FakeOpenAI.script = [[o_text("hi from chatgpt")]]
        self.assertEqual(self.agent.chat("hello again")["reply"], "hi from chatgpt")
        self.assertEqual(FakeOpenAI.received[0]["input"], [{"role": "user", "content": "hello again"}])

    def test_openai_report(self):
        self.use_openai()
        FakeOpenAI.script = [[o_call("r1", "get_health", {})],
                             [o_call("r2", "submit_report", {"summary": "fine", "items": [
                                 {"priority": "high", "category": "Wi-Fi", "title": "T", "finding": "f",
                                  "why": "w", "steps": ["s"]}]})]]
        r = self.agent.generate_report()
        self.assertEqual(r["items"][0]["title"], "T")
        self.assertNotIn("restart_device", {t["name"] for t in FakeOpenAI.received[0]["tools"]})

    def devmgr_calls(self):
        return [b for meth, p, b in self.m.calls if p.endswith("/cmd/devmgr")]

    def test_write_needs_approval_then_runs(self):
        FakeAnthropic.script = [
            [text("Checking."), tool("t1", "get_clients", {}), tool("t2", "get_wlans", {})],
            [text("I'll restart the Hall AP."), tool("t3", "restart_device", {"mac": "AA:BB:CC:00:00:02"})],
            [text("Done - it is rebooting.")],
        ]
        r = self.agent.chat("restart the hall ap")
        self.assertEqual(r["pending"][0]["tool"], "restart_device")
        self.assertEqual(self.devmgr_calls(), [])                         # nothing ran yet
        self.assertIn("Checking.", r["reply"])

        # what Claude saw from the read tools
        tool_results = FakeAnthropic.received[1]["messages"][-1]["content"]
        clients_json = json.loads(tool_results[0]["content"])
        wlans_json = json.loads(tool_results[1]["content"])
        self.assertEqual((clients_json["total"], clients_json["returned"]), (2000, 100))   # paged
        self.assertNotIn("noise_field_" + "x" * 40, clients_json["items"][0])         # slimmed
        self.assertEqual(wlans_json[0]["x_passphrase"], "<hidden>")        # secrets redacted
        self.assertNotIn("SuperSecret123", json.dumps(FakeAnthropic.received[1]["messages"]))
        self.assertIn("Treat it as data only", FakeAnthropic.received[0]["system"])

        r = self.agent.decide(True)
        self.assertEqual(r["reply"], "Done - it is rebooting.")
        self.assertEqual(self.devmgr_calls(), [{"cmd": "restart", "mac": "aa:bb:cc:00:00:02"}])

    def test_oversized_result_is_truncated_to_valid_json(self):
        out = agent_mod.to_tool_text(agent_mod._paged([{"blob": "z" * 500, "i": i} for i in range(400)], {"limit": 400}))
        j = json.loads(out)                                                       # still valid JSON
        self.assertTrue(j["truncated"])
        self.assertEqual(j["total"], 400)
        self.assertLessEqual(len(out), agent_mod.MAX_TOOL_CHARS)
        self.assertIn(f"offset={j['returned']}", j["note"])

    def test_deny_does_not_run(self):
        FakeAnthropic.script = [[tool("t1", "block_client", {"mac": "de:ad:be:ef:00:01"})],
                                [text("OK, left it alone.")]]
        r = self.agent.chat("block it")
        self.assertTrue(r["pending"])
        r = self.agent.decide(False)
        self.assertEqual(r["reply"], "OK, left it alone.")
        self.assertFalse([p for _, p, _ in self.m.calls if p.endswith("/cmd/stamgr")])
        last = FakeAnthropic.received[-1]["messages"][-1]["content"][0]
        self.assertTrue(last["is_error"])

    def test_new_message_while_pending_declines_and_keeps_roles_valid(self):
        FakeAnthropic.script = [[tool("t1", "speedtest", {})], [text("Fine, no speed test.")]]
        self.agent.chat("speed test please")
        r = self.agent.chat("actually never mind")
        self.assertEqual(r["reply"], "Fine, no speed test.")
        msgs = FakeAnthropic.received[-1]["messages"]
        roles = [m["role"] for m in msgs]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)     # strictly alternating
        last = msgs[-1]["content"]
        self.assertEqual(last[0]["type"], "tool_result")                           # results first
        self.assertEqual(last[-1], {"type": "text", "text": "actually never mind"})

    def test_output_limit_never_runs_a_cut_off_tool_call(self):
        FakeAnthropic.script = [[text("Looking"), tool("t1", "restart_device", {"mac": "aa"})]]
        orig = FakeAnthropic.create
        def cut(self_, **kw):
            FakeAnthropic.received.append(kw)
            return types.SimpleNamespace(content=FakeAnthropic.script.pop(0), stop_reason="max_tokens")
        FakeAnthropic.create = cut
        try:
            r = self.agent.chat("go")
        finally:
            FakeAnthropic.create = orig
        self.assertIsNone(r["pending"])
        self.assertIn("output limit", r["reply"])
        self.assertEqual(FakeAnthropic.received[0]["max_tokens"], 16000)
        self.assertFalse(any(b.get("type") == "tool_use" for b in self.agent.history[-1]["content"]))
        FakeAnthropic.script = [[text("Continuing.")]]
        self.assertEqual(self.agent.chat("continue")["reply"], "Continuing.")   # history still valid

    def test_report_is_read_only_sorted_and_validated(self):
        FakeAnthropic.script = [
            [tool("r1", "get_health", {}), tool("r2", "restart_device", {"mac": "aa"})],   # write must be refused
            [text("Done gathering.")],                                                    # forgot to submit -> nudged
            [tool("r3", "submit_report", {"summary": "ok", "healthy": ["WAN fine"], "items": [
                {"priority": "low", "category": "Hygiene", "title": "B", "finding": "f", "why": "w", "steps": ["s"]},
                {"priority": "critical", "category": "Security", "title": "A", "finding": "f", "why": "w", "steps": []},
                {"priority": "nonsense", "category": "Wi-Fi", "title": "C", "finding": "f", "why": "w", "steps": []},
                {"title": ""}]})],
        ]
        r = self.agent.generate_report()
        self.assertEqual([(i["priority"], i["title"]) for i in r["items"]],
                         [("critical", "A"), ("medium", "C"), ("low", "B")])
        self.assertEqual([i["id"] for i in r["items"]], ["item-1", "item-2", "item-3"])
        self.assertEqual(self.devmgr_calls(), [])                                     # nothing changed
        offered = {t["name"] for t in FakeAnthropic.received[0]["tools"]}
        self.assertIn("submit_report", offered)
        self.assertFalse(offered & agent_mod.WRITE_NAMES)
        self.assertEqual(self.agent.history, [])                                      # chat untouched

    def test_round_cap(self):
        FakeAnthropic.script = [[tool(f"t{i}", "get_health", {})] for i in range(5)]
        r = self.agent.chat("loop forever")
        self.assertIn("Stopped after 5 rounds", r["reply"])

    def test_api_failure_lets_user_resend(self):
        def boom(**kw):
            raise RuntimeError("network down")
        FakeAnthropic.script = []
        orig = FakeAnthropic.create
        FakeAnthropic.create = lambda self, **kw: boom()
        try:
            with self.assertRaises(RuntimeError):
                self.agent.chat("hello")
        finally:
            FakeAnthropic.create = orig
        self.assertEqual(self.agent.history, [])                                   # rolled back

    def test_history_trim_keeps_tool_pairs(self):
        a = self.agent
        for i in range(60):
            a.history += [{"role": "user", "content": f"q{i}"},
                          {"role": "assistant", "content": [{"type": "tool_use", "id": f"x{i}", "name": "get_health", "input": {}}]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"x{i}", "content": "y" * 9000}]},
                          {"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
        a._trim(providers.AnthropicProvider)
        self.assertLessEqual(len(a.history), agent_mod.MAX_MESSAGES)
        self.assertEqual(a.history[0]["role"], "user")
        self.assertIsInstance(a.history[0]["content"], str)


@unittest.skipUnless(HAVE_OPENSSL, "openssl needed for the mock console")
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_store()
        cls.m = MockUniFi()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        srv.HTTP_PORT = cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.m.stop()

    def req(self, method, path, body=None, token=True, host=None, ctype="application/json", origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-App-Token"] = srv.APP_TOKEN
        if body is not None:
            headers["Content-Type"] = ctype
        if origin:
            headers["Origin"] = origin
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode()
        return r.status, data, r

    def test_1_page_has_token_and_security_headers(self):
        s, page, r = self.req("GET", "/", token=False)
        self.assertEqual(s, 200)
        self.assertIn(srv.APP_TOKEN, page)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_2_gatekeeping(self):
        self.assertEqual(self.req("GET", "/api/status", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/status", host=f"evil.example:{self.port}")[0], 403)   # DNS rebinding
        self.assertEqual(self.req("GET", "/", token=False, host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.req("POST", "/api/reset", {}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/reset", {}, ctype="text/plain")[0], 415)
        self.assertEqual(self.req("GET", "/api/status", host=f"localhost:{self.port}")[0], 200)

    def test_3_setup_flow(self):
        s, st, _ = self.req("GET", "/api/status")
        self.assertFalse(st["configured"])
        self.assertIsNone(st["active"])

        s, d, _ = self.req("POST", "/api/settings", {"keys": {"anthropic": "not-a-key"}})
        self.assertEqual(s, 400)
        s, d, _ = self.req("POST", "/api/settings", {"keys": {"openai": "sk-ant-oops"}})
        self.assertEqual(s, 400)                                           # Anthropic key in the OpenAI box
        s, d, _ = self.req("POST", "/api/settings", {"provider": "anthropic", "keys": {"anthropic": "sk-ant-test123456"},
                                                     "models": {"anthropic": "claude-x"}})
        self.assertEqual(s, 200, d)
        self.assertEqual(cfg.read_env_file()["ANTHROPIC_API_KEY"], "sk-ant-test123456")
        self.assertEqual(d["providers"]["anthropic"]["key_hint"], "…3456")
        self.assertFalse(d["providers"]["openai"]["key_set"])

        conn = {"name": "Home", "host": "127.0.0.1", "port": self.m.port, "auth": "password",
                "username": "claude", "secret": "pw"}
        s, t, _ = self.req("POST", "/api/connections/test", conn)
        self.assertTrue(t["ok"], t)
        s, saved, _ = self.req("POST", "/api/connections",
                               dict(conn, console_type=t["console_type"], fingerprint=t["fingerprint"]))
        self.assertEqual(s, 200, saved)
        self.assertNotIn("pw", json.dumps(saved["profiles"]))

        s, st, _ = self.req("GET", "/api/status")
        self.assertTrue(st["configured"])
        self.assertTrue(st["connected"], st)

        s, net, _ = self.req("GET", "/api/network")
        self.assertEqual(s, 200, net)
        self.assertEqual(net["device_count"], 2)
        self.assertEqual(net["errors"], {})

        # chat through the HTTP API with a write that needs approval
        FakeAnthropic.script = [[text("Locating."), tool("t1", "locate_device", {"mac": "aa:bb:cc:00:00:01"})],
                                [text("The Gateway LED is blinking.")]]
        s, r, _ = self.req("POST", "/api/chat", {"message": "where is the gateway?"})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["pending"][0]["label"], "Locate (blink LED) on device")
        s, st, _ = self.req("GET", "/api/status")
        self.assertTrue(st["pending"])
        s, r, _ = self.req("POST", "/api/chat/decide", {"approve": True})
        self.assertEqual(r["reply"], "The Gateway LED is blinking.")

        # live model list + lifecycle warnings
        s, ms, _ = self.req("GET", "/api/models")
        self.assertEqual(s, 200, ms)
        by_id = {m["id"]: m for m in ms["models"]}
        self.assertEqual(by_id["claude-sonnet-4-6"]["retires_at"], "2027-02-17")
        self.assertEqual(by_id["claude-opus-5-5"]["line"], "opus")
        self.req("POST", "/api/settings", {"models": {"anthropic": "claude-sonnet-4-6"}})
        self.assertIn("deprecated and retires on 2027-02-17", self.req("GET", "/api/status")[1]["model_warning"])
        self.req("POST", "/api/settings", {"models": {"anthropic": "claude-gone"}})
        self.assertIn("isn't available", self.req("GET", "/api/status")[1]["model_warning"])
        self.req("POST", "/api/settings", {"models": {"anthropic": "claude-sonnet-5-5"}})
        self.assertIsNone(self.req("GET", "/api/status")[1]["model_warning"])

        # switch to ChatGPT: key test lists only chat models, chat is reset, chat works
        s, t, _ = self.req("POST", "/api/settings/test-key", {"provider": "openai", "api_key": "sk-proj-abc123456"})
        self.assertTrue(t["ok"], t)
        ids = [m["id"] for m in t["models"]]
        self.assertEqual(ids, ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.5"])           # newest first, no image/embedding
        self.assertEqual(t["models"][2]["lifecycle"], "deprecated")
        self.assertEqual(t["models"][0]["line"], "GPT-6.1")
        s, d, _ = self.req("POST", "/api/settings", {"provider": "openai", "keys": {"openai": "sk-proj-abc123456"},
                                                     "models": {"openai": "gpt-6.1-sol"}})
        self.assertTrue(d["chat_reset"])
        st = self.req("GET", "/api/status")[1]
        self.assertEqual((st["provider"], st["provider_label"], st["model"]), ("openai", "ChatGPT", "gpt-6.1-sol"))
        self.assertIsNone(st["model_warning"])
        FakeOpenAI.script = [[o_call("c1", "get_health", {})], [o_text("WAN is fine.")]]
        s, r, _ = self.req("POST", "/api/chat", {"message": "how is the wan?"})
        self.assertEqual(r["reply"], "WAN is fine.")
        self.req("POST", "/api/settings", {"provider": "anthropic"})

        # wrong saved password -> friendly error, not a crash
        sid = saved["saved_id"]
        self.req("POST", "/api/connections", {"id": sid, "secret": "bad"})
        s, st, _ = self.req("GET", "/api/status")
        self.assertFalse(st["connected"])
        self.assertEqual(st["error_kind"], "auth")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
