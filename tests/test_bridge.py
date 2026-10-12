import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from mcp_otel_bridge import otlp, proxy, sources
from mcp_otel_bridge import semconv as sc
from mcp_otel_bridge.cli import main, start_collector
from mcp_otel_bridge.demo import client_script_bytes, write_samples
from mcp_otel_bridge.view import stats, tree


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def by_name(spans):
    return {s.name: s for s in spans}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = write_samples(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, rows):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as fh:
            for r in rows:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return p


class HelperTests(unittest.TestCase):
    def test_to_ns_magnitudes(self):
        self.assertEqual(sources.to_ns(1760000000), 1760000000 * 10 ** 9)
        self.assertEqual(sources.to_ns(1760000000123), 1760000000123 * 10 ** 6)
        self.assertEqual(sources.to_ns(1760000000123456789), 1760000000123456789)
        self.assertEqual(sources.to_ns("2025-10-09T08:53:20Z"), 1760000000 * 10 ** 9)
        self.assertIsNone(sources.to_ns("not a time"))
        self.assertIsNone(sources.to_ns(True))

    def test_any_value(self):
        self.assertEqual(otlp.any_value(True), {"boolValue": True})
        self.assertEqual(otlp.any_value(5), {"intValue": "5"})
        self.assertEqual(otlp.any_value(0.5), {"doubleValue": 0.5})
        self.assertEqual(otlp.any_value(["a"]), {"arrayValue": {"values": [{"stringValue": "a"}]}})

    def test_seeded_ids_reproducible(self):
        a, b = otlp.IdGen("s"), otlp.IdGen("s")
        self.assertEqual([a.trace_id(), a.span_id()], [b.trace_id(), b.span_id()])
        self.assertRegex(otlp.IdGen().trace_id(), r"^[0-9a-f]{32}$")

    def test_redact(self):
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.assertNotIn(key, otlp.redact("key=" + key))
        self.assertEqual(otlp.redact("Authorization: Bearer abcdefghijklmnop123"),
                         "Authorization: Bearer [REDACTED]")

    def test_validate_catches_problems(self):
        good = otlp.payload([otlp.Span("x", "a" * 32, "b" * 16, 10, 20)], "svc")
        self.assertEqual(otlp.validate(good), [])
        bad = otlp.payload([otlp.Span("", "A" * 32, "b" * 16, 20, 10, parent_id="c" * 16)], "svc")
        bad["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"] = "10"
        probs = " ".join(otlp.validate(bad))
        for frag in ("traceId", "name is empty", "ends before", "parent"):
            self.assertIn(frag, probs)
        self.assertTrue(otlp.validate({"nope": 1}))


class GooseTests(Base):
    def test_spans(self):
        fmt, spans = sources.convert(self.paths["goose"], ids=otlp.IdGen("t"))
        self.assertEqual(fmt, "goose")
        names = by_name(spans)
        root = names["invoke_agent goose"]
        self.assertEqual(root.attributes[sc.GEN_AI_OPERATION_NAME], "invoke_agent")
        self.assertEqual(root.attributes[sc.GEN_AI_CONVERSATION_ID], "20261012_demo")
        self.assertEqual(root.attributes[sc.GEN_AI_USAGE_INPUT_TOKENS], 5120)
        shell = names["execute_tool developer__shell"]
        self.assertEqual(shell.parent_id, root.span_id)
        self.assertEqual(shell.duration_ms, 4000.0)
        self.assertEqual(shell.attributes[sc.BRIDGE_EXTENSION], "developer")
        self.assertNotIn(sc.GEN_AI_TOOL_CALL_ARGUMENTS, shell.attributes)
        pr = names["execute_tool github__create_pull_request"]
        self.assertEqual((pr.status, pr.attributes[sc.ERROR_TYPE]), (otlp.STATUS_ERROR, "tool_error"))

    def test_capture_content(self):
        _, spans = sources.convert(self.paths["goose"], capture=True)
        shell = by_name(spans)["execute_tool developer__shell"]
        self.assertIn("pytest", shell.attributes[sc.GEN_AI_TOOL_CALL_ARGUMENTS])
        self.assertIn("1 failed", shell.attributes[sc.GEN_AI_TOOL_CALL_RESULT])

    def test_unanswered_call(self):
        p = self.write("s.jsonl", [{"role": "assistant", "created": 1, "content": [
            {"type": "toolRequest", "id": "x", "toolCall": {"value": {"name": "a__b", "arguments": {}}}}]}])
        _, spans = sources.convert(p, "goose")
        self.assertEqual(by_name(spans)["execute_tool a__b"].attributes[sc.ERROR_TYPE], "no_response")


class JsonRpcTests(Base):
    def test_cassette(self):
        fmt, spans = sources.convert(self.paths["replay"], ids=otlp.IdGen("t"))
        self.assertEqual(fmt, "jsonrpc")
        root = spans[0]
        self.assertEqual(root.name, "mcp.session weather")
        self.assertEqual(len(root.events), 1)  # notifications/initialized
        calls = [s for s in spans if s.attributes.get(sc.MCP_METHOD_NAME) == "tools/call"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].attributes[sc.GEN_AI_TOOL_NAME], "forecast")
        self.assertEqual(calls[0].kind, otlp.KIND_CLIENT)
        self.assertAlmostEqual(calls[0].duration_ms, 380.0, places=3)
        self.assertEqual(calls[1].attributes[sc.ERROR_TYPE], "tool_error")
        rr = [s for s in spans if s.name == "resources/read weather://alerts"][0]
        self.assertEqual(rr.attributes[sc.ERROR_TYPE], "-32002")
        self.assertEqual(rr.attributes[sc.MCP_RESOURCE_URI], "weather://alerts")
        self.assertTrue(all(s.attributes[sc.MCP_PROTOCOL_VERSION] == "2025-11-25" for s in spans))

    def test_bare_messages_without_envelope(self):
        p = self.write("bare.jsonl", [
            {"jsonrpc": "2.0", "id": "a", "method": "tools/call", "params": {"name": "t"}},
            {"jsonrpc": "2.0", "id": "a", "result": {"content": []}},
            {"jsonrpc": "2.0", "id": "b", "method": "ping"},
        ])
        _, spans = sources.convert(p)
        n = by_name(spans)
        self.assertEqual(n["tools/call t"].status, otlp.STATUS_OK)
        self.assertEqual(n["ping"].attributes[sc.ERROR_TYPE], "no_response")

    def test_server_initiated_request(self):
        p = self.write("s.jsonl", [
            {"direction": "s2c", "t": 1, "message": {"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage"}},
            {"direction": "c2s", "t": 2, "message": {"jsonrpc": "2.0", "id": 1, "result": {}}},
        ])
        _, spans = sources.convert(p)
        sp = by_name(spans)["sampling/createMessage"]
        self.assertEqual(sp.kind, otlp.KIND_SERVER)
        self.assertEqual(sp.status, otlp.STATUS_OK)

    def test_string_wrapped_message(self):
        p = self.write("w.jsonl", [{"msg": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})}])
        self.assertEqual(sources.convert(p)[0], "jsonrpc")


class AuditTests(Base):
    def test_audit(self):
        fmt, spans = sources.convert(self.paths["gateway"])
        self.assertEqual(fmt, "audit")
        shell = [s for s in spans if s.name == "tools/call run_shell"][0]
        self.assertEqual(shell.attributes[sc.ERROR_TYPE], "policy_denied")
        self.assertEqual(shell.attributes[sc.BRIDGE_REASON], "deny-list: run_shell")
        monitored = [s for s in spans if s.name == "tools/call http_get"][0]
        self.assertEqual(monitored.status, otlp.STATUS_OK)
        self.assertEqual(monitored.events[0]["name"], "guardrail.would_block")
        read = [s for s in spans if s.name == "tools/call read_file"][0]
        self.assertAlmostEqual(read.duration_ms, 4.0)
        self.assertEqual(spans[0].attributes["mcp_bridge.guardrail.blocked"], 1)


class DetectAndErrors(Base):
    def test_unknown_format(self):
        p = self.write("x.jsonl", [{"hello": "world"}])
        with self.assertRaises(sources.SourceError):
            sources.convert(p)

    def test_invalid_json(self):
        p = self.write("x.jsonl", ["{oops"])
        code, _, err = run(["convert", p])
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)


class ProxyTests(unittest.TestCase):
    def test_proxy_round_trip(self):
        out = io.BytesIO()
        code, records = proxy.run([sys.executable, "-m", "mcp_otel_bridge.sample_server"],
                                  stdin=io.BytesIO(client_script_bytes()), stdout=out)
        self.assertEqual(code, 0)
        replies = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5, 6])  # passthrough intact
        rows = proxy.as_jsonl_records(records)
        spans = sources.jsonrpc_spans(rows, otlp.IdGen("p"), rows[0][1]["ts"], source="proxy")
        n = {s.name: s for s in spans}
        self.assertIn("mcp.session calc-sample", n)
        self.assertEqual(n["tools/call divide"].attributes[sc.ERROR_TYPE], "tool_error")
        self.assertEqual(n["tools/call nope"].attributes[sc.ERROR_TYPE], "-32602")
        self.assertGreaterEqual(n["tools/call slow"].duration_ms, 40)
        self.assertEqual(otlp.validate(otlp.payload(spans, "t")), [])


class CliTests(Base):
    def test_convert_to_file_and_view(self):
        out = os.path.join(self.tmp.name, "t.json")
        code, _, err = run(["convert", self.paths["replay"], "-o", out, "--seed", "x"])
        self.assertEqual(code, 0)
        self.assertIn("6 span(s)", err)
        code, view, _ = run(["view", out])
        self.assertEqual(code, 0)
        self.assertIn("tools/call forecast", view)
        self.assertIn("2 tool call(s)", view)

    def test_seed_makes_output_identical(self):
        a = run(["convert", self.paths["goose"], "--seed", "s"])[1]
        b = run(["convert", self.paths["goose"], "--seed", "s"])[1]
        self.assertEqual(a, b)

    def test_fail_on_error(self):
        self.assertEqual(run(["convert", self.paths["replay"], "--fail-on-error"])[0], 1)

    def test_post_to_collector(self):
        url, srv, received = start_collector()
        try:
            code, _, err = run(["convert", self.paths["gateway"], "--endpoint", url, "--header", "x-key: 1"])
            self.assertEqual(code, 0)
            self.assertIn("HTTP 200", err)
            self.assertEqual(len(list(otlp.iter_spans(received[0]))), 5)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_view_rejects_invalid(self):
        p = os.path.join(self.tmp.name, "bad.json")
        with open(p, "w") as fh:
            json.dump({"resourceSpans": [{"scopeSpans": [{"spans": [
                {"traceId": "x", "spanId": "y", "name": "n", "startTimeUnixNano": "1",
                 "endTimeUnixNano": "2"}]}]}]}, fh)
        self.assertEqual(run(["view", p])[0], 1)

    def test_tree_and_stats(self):
        _, spans = sources.convert(self.paths["replay"])
        doc = otlp.payload(spans, "svc")
        t = tree(doc)
        self.assertTrue(t.splitlines()[0].startswith("mcp.session weather"))
        self.assertIn("`- resources/read", t)
        self.assertEqual(stats(doc)["tools"], {"forecast": 2})

    def test_demo(self):
        code, out, _ = run(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("received 4 payload(s), 22 span(s)", out)


if __name__ == "__main__":
    unittest.main()
