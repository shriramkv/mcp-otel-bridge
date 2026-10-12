"""Turn logs into spans.

Three sources:

* ``goose``   goose session JSONL (agent turns and tool calls)
* ``jsonrpc`` any JSONL stream of MCP JSON-RPC messages, such as an
              mcp-replay cassette or the bridge's own proxy recording
* ``audit``   guardrail audit JSONL, such as mcp-guardrail-gateway's audit trail

Readers are lenient about field names because each tool writes slightly
different envelopes; see the README for the fields that are recognised.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import semconv as sc
from .otlp import (
    KIND_CLIENT, KIND_INTERNAL, KIND_SERVER, STATUS_ERROR, STATUS_OK, IdGen, Span, redact,
)

MAX_CONTENT = 2048
MS = 1_000_000


class SourceError(ValueError):
    pass


# ----------------------------------------------------------------- helpers

def to_ns(value: Any) -> Optional[int]:
    """Seconds, ms, us, ns (by magnitude) or an ISO-8601 string -> unix ns."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):  # exact integer arithmetic, no float rounding
        if value > 10 ** 17:
            return value
        if value > 10 ** 14:
            return value * 1000
        if value > 10 ** 11:
            return value * 1000000
        return value * 1000000000
    if isinstance(value, float):
        v = value
        if v > 1e17:
            return int(v)
        if v > 1e14:
            return int(v * 1e3)
        if v > 1e11:
            return int(v * 1e6)
        return int(v * 1e9)
    if isinstance(value, str):
        s = value.strip()
        try:
            return to_ns(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)
    return None


def _first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _clip(obj: Any) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
    text = redact(text)
    return text if len(text) <= MAX_CONTENT else text[:MAX_CONTENT] + "...[truncated]"


class Clock:
    """Fills in missing timestamps: 1 ms after the last known one."""

    def __init__(self, start_ns: int):
        self.last = start_ns

    def at(self, ns: Optional[int]) -> int:
        if ns is None:
            self.last += MS
        else:
            self.last = max(ns, 0)
        return self.last


def read_jsonl(lines: Iterable[str]) -> List[Tuple[int, dict]]:
    out = []
    for n, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError("line %d: invalid JSON (%s)" % (n, exc.msg))
        if isinstance(obj, dict):
            out.append((n, obj))
        elif isinstance(obj, list):
            out.extend((n, o) for o in obj if isinstance(o, dict))
    return out


# ----------------------------------------------------------------- detection

def _is_jsonrpc(o: dict) -> bool:
    return o.get("jsonrpc") == "2.0" or "method" in o or ("id" in o and ("result" in o or "error" in o))


def _unwrap(obj: dict) -> Optional[dict]:
    for k in ("message", "msg", "payload", "data", "frame", "json"):
        v = obj.get(k)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict) and _is_jsonrpc(v):
            return v
    if _is_jsonrpc(obj) and "role" not in obj:
        return obj
    return None


def detect(records: List[Tuple[int, dict]]) -> str:
    for _, o in records[:50]:
        if "role" in o and isinstance(o.get("content"), list):
            return "goose"
        if _first(o, "decision", "verdict", "action") is not None and _first(o, "tool", "tool_name", "name"):
            return "audit"
        if _unwrap(o) is not None:
            return "jsonrpc"
    raise SourceError("could not tell the log format; pass --from goose|jsonrpc|audit")


# ----------------------------------------------------------------- jsonrpc

def _direction(o: dict) -> Optional[str]:
    v = _first(o, "direction", "dir", "from", "source", "stream", "side")
    if not isinstance(v, str):
        return None
    s = v.lower()
    if any(t in s for t in ("c2s", "client->", "client_to", "to_server", "request", "send", "outbound")) or s in (
            ">", "->", "client", "stdin"):
        return "c2s"
    if any(t in s for t in ("s2c", "server->", "server_to", "to_client", "response", "recv", "inbound")) or s in (
            "<", "<-", "server", "stdout"):
        return "s2c"
    return None


def _target(method: str, params: dict) -> str:
    if method in ("tools/call", "prompts/get"):
        return str(params.get("name") or "")
    if method in ("resources/read", "resources/subscribe", "resources/unsubscribe"):
        return str(params.get("uri") or "")
    return ""


def jsonrpc_spans(records: List[Tuple[int, dict]], ids: IdGen, start_ns: int, capture: bool = False,
                  session_id: Optional[str] = None, source: str = "jsonrpc",
                  transport: str = "pipe") -> List[Span]:
    """Pair requests with responses and build one span per request under a session root."""
    clock = Clock(start_ns)
    msgs = []
    for _, o in records:
        m = _unwrap(o)
        if m is None:
            continue
        ts = clock.at(to_ns(_first(o, "ts", "timestamp", "time", "t", "at", "created")))
        msgs.append((ts, _direction(o), m))
    if not msgs:
        raise SourceError("no JSON-RPC messages found")

    trace = ids.trace_id()
    root = Span("mcp.session", trace, ids.span_id(), msgs[0][0], msgs[-1][0], kind=KIND_INTERNAL,
                attributes={sc.BRIDGE_SOURCE: source, sc.NETWORK_TRANSPORT: transport,
                            sc.JSONRPC_PROTOCOL_VERSION: "2.0"})
    if session_id:
        root.attributes[sc.MCP_SESSION_ID] = session_id
    spans = [root]
    pending: Dict[Tuple[str, str], Span] = {}
    protocol = None
    server_name = None
    errors = 0

    for ts, direction, m in msgs:
        method = m.get("method")
        has_id = "id" in m and m.get("id") is not None
        if method and has_id:
            side = direction or "c2s"
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            target = _target(method, params)
            sp = Span((method + " " + target).strip(), trace, ids.span_id(), ts, ts, parent_id=root.span_id,
                      kind=KIND_CLIENT if side == "c2s" else KIND_SERVER)
            a = sp.attributes
            a[sc.MCP_METHOD_NAME] = method
            a[sc.JSONRPC_REQUEST_ID] = str(m["id"])
            a[sc.NETWORK_TRANSPORT] = transport
            if session_id:
                a[sc.MCP_SESSION_ID] = session_id
            if method == "tools/call":
                a[sc.GEN_AI_OPERATION_NAME] = sc.OP_EXECUTE_TOOL
                a[sc.GEN_AI_TOOL_NAME] = target
                if capture and "arguments" in params:
                    a[sc.GEN_AI_TOOL_CALL_ARGUMENTS] = _clip(params["arguments"])
            elif method == "prompts/get":
                a[sc.GEN_AI_PROMPT_NAME] = target
            elif method.startswith("resources/") and target:
                a[sc.MCP_RESOURCE_URI] = target
            elif method == "initialize" and params.get("protocolVersion"):
                protocol = params["protocolVersion"]
            pending[(side, str(m["id"]))] = sp
            spans.append(sp)
        elif method:  # notification
            root.add_event(method, ts, {sc.MCP_METHOD_NAME: method,
                                        sc.BRIDGE_DIRECTION: direction or "unknown"})
        elif has_id:
            rid = str(m["id"])
            if direction == "s2c":
                sp = pending.pop(("c2s", rid), None)
            elif direction == "c2s":
                sp = pending.pop(("s2c", rid), None)
            else:
                sp = pending.pop(("c2s", rid), None) or pending.pop(("s2c", rid), None)
            if sp is None:
                continue
            sp.end_ns = max(ts, sp.start_ns)
            if isinstance(m.get("error"), dict):
                code = m["error"].get("code")
                sp.error(str(code), str(m["error"].get("message", ""))[:200])
                sp.attributes[sc.RPC_RESPONSE_STATUS_CODE] = str(code)
                errors += 1
            else:
                result = m.get("result") if isinstance(m.get("result"), dict) else {}
                if result.get("isError") is True:
                    sp.error(sc.ERR_TOOL, "tool returned isError")
                    errors += 1
                else:
                    sp.status = STATUS_OK
                if capture and sp.attributes.get(sc.MCP_METHOD_NAME) == "tools/call":
                    sp.attributes[sc.GEN_AI_TOOL_CALL_RESULT] = _clip(result.get("content", result))
                if sp.attributes.get(sc.MCP_METHOD_NAME) == "initialize":
                    protocol = result.get("protocolVersion") or protocol
                    info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
                    server_name = info.get("name") or server_name

    for sp in pending.values():
        sp.error(sc.ERR_NO_RESPONSE, "no response in the log")
        errors += 1
    if protocol:
        for sp in spans:
            sp.attributes[sc.MCP_PROTOCOL_VERSION] = protocol
    if server_name:
        root.name = "mcp.session " + server_name
        root.attributes[sc.BRIDGE_SERVER_NAME] = server_name
    root.end_ns = max(sp.end_ns for sp in spans)
    root.status = STATUS_ERROR if errors else STATUS_OK
    if errors:
        root.status_message = "%d request(s) failed" % errors
    return spans


# ----------------------------------------------------------------- goose

def goose_spans(records: List[Tuple[int, dict]], ids: IdGen, start_ns: int, capture: bool = False,
                session_id: Optional[str] = None, fallback_id: Optional[str] = None) -> List[Span]:
    clock = Clock(start_ns)
    meta: dict = {}
    trace = ids.trace_id()
    root: Optional[Span] = None
    spans: List[Span] = []
    calls: Dict[str, Span] = {}
    messages = 0
    errors = 0

    for _, o in records:
        if "role" not in o:
            if not meta and root is None:
                meta = o
            continue
        content = o.get("content") if isinstance(o.get("content"), list) else []
        ts = clock.at(to_ns(o.get("created")))
        messages += 1
        if root is None:
            root = Span("invoke_agent goose", trace, ids.span_id(), ts, ts, kind=KIND_INTERNAL,
                        attributes={sc.GEN_AI_OPERATION_NAME: sc.OP_INVOKE_AGENT, sc.GEN_AI_AGENT_NAME: "goose",
                                    sc.BRIDGE_SOURCE: "goose"})
            spans.append(root)
        root.end_ns = max(root.end_ns, ts)
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "toolRequest":
                call = item.get("toolCall") if isinstance(item.get("toolCall"), dict) else {}
                value = call.get("value") if isinstance(call.get("value"), dict) else {}
                name = str(value.get("name") or "unknown")
                cid = str(item.get("id") or "call-%d" % len(calls))
                sp = Span("execute_tool " + name, trace, ids.span_id(), ts, ts, parent_id=root.span_id,
                          kind=KIND_INTERNAL, attributes={
                              sc.GEN_AI_OPERATION_NAME: sc.OP_EXECUTE_TOOL,
                              sc.GEN_AI_TOOL_NAME: name,
                              sc.GEN_AI_TOOL_CALL_ID: cid,
                          })
                if "__" in name:
                    sp.attributes[sc.BRIDGE_EXTENSION] = name.split("__", 1)[0]
                if capture and "arguments" in value:
                    sp.attributes[sc.GEN_AI_TOOL_CALL_ARGUMENTS] = _clip(value["arguments"])
                if call.get("status") == "error":
                    sp.error("invalid_tool_call", "goose could not parse the tool call")
                    errors += 1
                calls[cid] = sp
                spans.append(sp)
            elif itype == "toolResponse":
                cid = str(item.get("id") or "")
                sp = calls.pop(cid, None)
                if sp is None:
                    continue
                sp.end_ns = max(ts, sp.start_ns)
                result = item.get("toolResult") if isinstance(item.get("toolResult"), dict) else {}
                is_error = result.get("status") == "error" or (
                    isinstance(result.get("value"), dict) and result["value"].get("isError") is True)
                if is_error:
                    sp.error(sc.ERR_TOOL, str(result.get("error") or "tool error")[:200])
                    errors += 1
                elif sp.status != STATUS_ERROR:
                    sp.status = STATUS_OK
                if capture:
                    sp.attributes[sc.GEN_AI_TOOL_CALL_RESULT] = _clip(result.get("value", result))
    if root is None:
        raise SourceError("no goose messages found")
    for sp in calls.values():
        sp.error(sc.ERR_NO_RESPONSE, "no tool response in the session")
        errors += 1
    sid = session_id or meta.get("id") or meta.get("session_id") or fallback_id
    if sid:
        root.attributes[sc.GEN_AI_CONVERSATION_ID] = str(sid)
    if meta.get("description"):
        root.attributes["mcp_bridge.goose.description"] = str(meta["description"])[:200]
    for key, attr in (("input_tokens", sc.GEN_AI_USAGE_INPUT_TOKENS), ("output_tokens", sc.GEN_AI_USAGE_OUTPUT_TOKENS),
                      ("accumulated_input_tokens", sc.GEN_AI_USAGE_INPUT_TOKENS),
                      ("accumulated_output_tokens", sc.GEN_AI_USAGE_OUTPUT_TOKENS)):
        if isinstance(meta.get(key), int) and attr not in root.attributes:
            root.attributes[attr] = meta[key]
    root.attributes[sc.BRIDGE_MESSAGES] = messages
    root.end_ns = max(sp.end_ns for sp in spans)
    root.status = STATUS_ERROR if errors else STATUS_OK
    if errors:
        root.status_message = "%d tool call(s) failed" % errors
    return spans


# ----------------------------------------------------------------- audit

_DENY = ("deny", "denied", "block", "blocked", "reject", "rejected")


def audit_spans(records: List[Tuple[int, dict]], ids: IdGen, start_ns: int,
                session_id: Optional[str] = None) -> List[Span]:
    clock = Clock(start_ns)
    trace = ids.trace_id()
    spans: List[Span] = []
    root: Optional[Span] = None
    blocked = 0
    for _, o in records:
        tool = _first(o, "tool", "tool_name", "name")
        decision = _first(o, "decision", "verdict", "action")
        if tool is None and decision is None:
            continue
        ts = clock.at(to_ns(_first(o, "ts", "timestamp", "time", "t", "at")))
        if root is None:
            root = Span("mcp.guardrail.session", trace, ids.span_id(), ts, ts, kind=KIND_INTERNAL,
                        attributes={sc.BRIDGE_SOURCE: "audit"})
            if session_id:
                root.attributes[sc.MCP_SESSION_ID] = session_id
            spans.append(root)
        dur = _first(o, "duration_ms", "latency_ms", "elapsed_ms")
        end = ts + int(float(dur) * MS) if isinstance(dur, (int, float)) else ts
        method = str(_first(o, "method") or "tools/call")
        sp = Span((method + " " + str(tool or "")).strip(), trace, ids.span_id(), ts, end,
                  parent_id=root.span_id, kind=KIND_INTERNAL, attributes={
                      sc.MCP_METHOD_NAME: method,
                      sc.BRIDGE_DECISION: str(decision) if decision is not None else None,
                      sc.BRIDGE_REASON: _first(o, "reason", "rule", "message"),
                  })
        if tool is not None and method == "tools/call":
            sp.attributes[sc.GEN_AI_TOOL_NAME] = str(tool)
            sp.attributes[sc.GEN_AI_OPERATION_NAME] = sc.OP_EXECUTE_TOOL
        rid = _first(o, "id", "request_id", "jsonrpc_id")
        if rid is not None:
            sp.attributes[sc.JSONRPC_REQUEST_ID] = str(rid)
        enforced = o.get("enforced")
        if isinstance(enforced, bool):
            sp.attributes[sc.BRIDGE_ENFORCED] = enforced
        if str(decision).lower() in _DENY:
            if enforced is False:
                sp.add_event("guardrail.would_block", ts, {sc.BRIDGE_REASON: sp.attributes.get(sc.BRIDGE_REASON)})
                sp.status = STATUS_OK
            else:
                sp.error(sc.ERR_POLICY_DENIED, str(sp.attributes.get(sc.BRIDGE_REASON) or "blocked by policy"))
                blocked += 1
        else:
            sp.status = STATUS_OK
        spans.append(sp)
        root.end_ns = max(root.end_ns, end)
    if root is None:
        raise SourceError("no audit entries found")
    root.attributes["mcp_bridge.guardrail.blocked"] = blocked
    root.status = STATUS_OK
    return spans


# ----------------------------------------------------------------- entry point

def convert(path: str, fmt: str = "auto", ids: Optional[IdGen] = None, capture: bool = False,
            session_id: Optional[str] = None, start_ns: Optional[int] = None) -> Tuple[str, List[Span]]:
    with open(path, "r", encoding="utf-8") as fh:
        records = read_jsonl(fh)
    if not records:
        raise SourceError("%s is empty" % path)
    ids = ids or IdGen()
    if start_ns is None:
        start_ns = int(os.path.getmtime(path) * 1e9)
    fmt = detect(records) if fmt == "auto" else fmt
    if fmt == "goose":
        stem = os.path.splitext(os.path.basename(path))[0]
        return fmt, goose_spans(records, ids, start_ns, capture, session_id, fallback_id=stem)
    if fmt == "jsonrpc":
        return fmt, jsonrpc_spans(records, ids, start_ns, capture, session_id)
    if fmt == "audit":
        return fmt, audit_spans(records, ids, start_ns, session_id)
    raise SourceError("unknown format %r" % fmt)
