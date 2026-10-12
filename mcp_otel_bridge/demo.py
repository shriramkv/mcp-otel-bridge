"""Bundled sample logs, one per source, with fixed timestamps."""

from __future__ import annotations

import json
import os

T0 = 1760000000  # seconds


def _goose():
    lines = [{"id": "20261012_demo", "description": "fix the failing cart test", "working_dir": "/work/shop",
              "message_count": 7, "input_tokens": 5120, "output_tokens": 830}]

    def req(t, cid, name, args):
        return {"role": "assistant", "created": t, "content": [{"type": "toolRequest", "id": cid, "toolCall": {
            "status": "success", "value": {"name": name, "arguments": args}}}]}

    def resp(t, cid, text, status="success"):
        res = {"status": status, "value": [{"type": "text", "text": text}]}
        if status == "error":
            res = {"status": "error", "error": text}
        return {"role": "user", "created": t, "content": [{"type": "toolResponse", "id": cid, "toolResult": res}]}

    lines += [
        {"role": "user", "created": T0, "content": [{"type": "text", "text": "Fix the failing cart test"}]},
        req(T0 + 2, "c1", "developer__shell", {"command": "pytest -q tests/test_cart.py"}),
        resp(T0 + 6, "c1", "1 failed, 11 passed"),
        req(T0 + 9, "c2", "developer__text_editor", {"command": "str_replace", "path": "src/cart.py"}),
        resp(T0 + 9, "c2", "edited"),
        req(T0 + 10, "c3", "github__create_pull_request", {"title": "Fix cart rounding"}),
        resp(T0 + 12, "c3", "403: resource not accessible by integration", status="error"),
        {"role": "assistant", "created": T0 + 13, "content": [{"type": "text", "text": "Fixed; PR creation failed."}]},
    ]
    return lines


def _cassette():
    def c2s(t, m):
        return {"ts": T0 + t, "direction": "client->server", "message": m}

    def s2c(t, m):
        return {"ts": T0 + t, "direction": "server->client", "message": m}

    return [
        c2s(0.000, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
        s2c(0.012, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25",
                                                          "serverInfo": {"name": "weather"}}}),
        c2s(0.015, {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        c2s(0.020, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        s2c(0.031, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}),
        c2s(0.100, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "forecast", "arguments": {"city": "Coimbatore"}}}),
        s2c(0.480, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "31C"}]}}),
        c2s(0.500, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "forecast", "arguments": {"city": ""}}}),
        s2c(0.530, {"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "city required"}],
                                                          "isError": True}}),
        c2s(0.600, {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "weather://alerts"}}),
        s2c(0.610, {"jsonrpc": "2.0", "id": 5, "error": {"code": -32002, "message": "resource not found"}}),
    ]


def _audit():
    return [
        {"ts": T0, "tool": "read_file", "decision": "allow", "duration_ms": 4, "id": 11},
        {"ts": T0 + 1, "tool": "run_shell", "decision": "deny", "reason": "deny-list: run_shell", "enforced": True,
         "id": 12},
        {"ts": T0 + 2, "tool": "http_get", "decision": "deny", "reason": "guard: url matches internal host",
         "enforced": False, "id": 13},
        {"ts": T0 + 3, "tool": "read_file", "decision": "allow", "duration_ms": 6, "id": 14},
    ]


def write_samples(dirpath: str) -> dict:
    os.makedirs(dirpath, exist_ok=True)
    out = {}
    for name, rows in (("goose-session.jsonl", _goose()), ("replay-cassette.jsonl", _cassette()),
                       ("gateway-audit.jsonl", _audit())):
        p = os.path.join(dirpath, name)
        with open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        out[name.split("-")[0]] = p
    return out


CLIENT_SCRIPT = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 2, "b": 3}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "slow", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "divide", "arguments": {"a": 1, "b": 0}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
]


def client_script_bytes() -> bytes:
    return "".join(json.dumps(m) + "\n" for m in CLIENT_SCRIPT).encode("utf-8")
