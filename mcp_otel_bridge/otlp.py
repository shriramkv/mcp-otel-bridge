"""Spans, IDs and the OTLP/JSON wire format (traces only).

OTLP/JSON rules followed here: trace and span IDs are lowercase hex strings,
64-bit integers (timestamps) are decimal strings, enums are integers, attribute
values are wrapped AnyValue objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import __version__

KIND_INTERNAL, KIND_SERVER, KIND_CLIENT = 1, 2, 3
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

_SECRETS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
]


def redact(text: str) -> str:
    for rx in _SECRETS:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class IdGen:
    """Random IDs by default; reproducible IDs when given a seed (tests, golden files)."""

    def __init__(self, seed: Optional[str] = None):
        self.seed = seed
        self.n = 0

    def _hex(self, nbytes: int) -> str:
        if self.seed is None:
            return os.urandom(nbytes).hex()
        self.n += 1
        return hashlib.sha256(("%s:%d" % (self.seed, self.n)).encode()).hexdigest()[: nbytes * 2]

    def trace_id(self) -> str:
        return self._hex(16)

    def span_id(self) -> str:
        return self._hex(8)


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    start_ns: int
    end_ns: int
    parent_id: str = ""
    kind: int = KIND_INTERNAL
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: int = STATUS_UNSET
    status_message: str = ""
    events: List[dict] = field(default_factory=list)

    def error(self, error_type: str, message: str = "") -> None:
        self.status = STATUS_ERROR
        self.status_message = message
        self.attributes["error.type"] = error_type

    def add_event(self, name: str, time_ns: int, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({"name": name, "time_ns": time_ns, "attributes": attributes or {}})

    @property
    def duration_ms(self) -> float:
        return max(0, self.end_ns - self.start_ns) / 1e6


def any_value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [any_value(x) for x in v]}}
    return {"stringValue": str(v)}


def attrs(d: Dict[str, Any]) -> List[dict]:
    return [{"key": k, "value": any_value(v)} for k, v in d.items() if v is not None]


def span_to_otlp(s: Span) -> dict:
    out = {
        "traceId": s.trace_id,
        "spanId": s.span_id,
        "name": s.name,
        "kind": s.kind,
        "startTimeUnixNano": str(s.start_ns),
        "endTimeUnixNano": str(max(s.end_ns, s.start_ns)),
        "attributes": attrs(s.attributes),
        "status": {"code": s.status},
    }
    if s.parent_id:
        out["parentSpanId"] = s.parent_id
    if s.status_message:
        out["status"]["message"] = s.status_message
    if s.events:
        out["events"] = [{"name": e["name"], "timeUnixNano": str(e["time_ns"]),
                          "attributes": attrs(e["attributes"])} for e in s.events]
    return out


def payload(spans: List[Span], service_name: str, resource: Optional[Dict[str, Any]] = None) -> dict:
    res = {"service.name": service_name, "telemetry.sdk.name": "mcp-otel-bridge",
           "telemetry.sdk.language": "python", "telemetry.sdk.version": __version__}
    res.update(resource or {})
    return {"resourceSpans": [{
        "resource": {"attributes": attrs(res)},
        "scopeSpans": [{
            "scope": {"name": "mcp-otel-bridge", "version": __version__},
            "spans": [span_to_otlp(s) for s in spans],
        }],
    }]}


def iter_spans(doc: dict):
    for rs in doc.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                yield sp


def validate(doc: dict) -> List[str]:
    """Structural checks an OTLP receiver would reject or mis-render. [] means valid."""
    problems = []
    if not isinstance(doc, dict) or not isinstance(doc.get("resourceSpans"), list):
        return ["top level must be an object with a resourceSpans array"]
    ids = set()
    spans = list(iter_spans(doc))
    for i, sp in enumerate(spans):
        where = "span %d (%s)" % (i, sp.get("name", "?"))
        if not re.fullmatch(r"[0-9a-f]{32}", str(sp.get("traceId", ""))) or sp["traceId"] == "0" * 32:
            problems.append(where + ": traceId must be 32 lowercase hex chars, not all zero")
        if not re.fullmatch(r"[0-9a-f]{16}", str(sp.get("spanId", ""))) or sp["spanId"] == "0" * 16:
            problems.append(where + ": spanId must be 16 lowercase hex chars, not all zero")
        if not sp.get("name"):
            problems.append(where + ": name is empty")
        try:
            st, en = int(sp.get("startTimeUnixNano")), int(sp.get("endTimeUnixNano"))
            if en < st:
                problems.append(where + ": ends before it starts")
        except (TypeError, ValueError):
            problems.append(where + ": timestamps must be integer nanoseconds")
        ids.add((sp.get("traceId"), sp.get("spanId")))
    for i, sp in enumerate(spans):
        p = sp.get("parentSpanId")
        if p and (sp.get("traceId"), p) not in ids:
            problems.append("span %d (%s): parent %s not in this payload" % (i, sp.get("name"), p))
    return problems


def post(endpoint: str, doc: dict, headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> int:
    """POST to an OTLP/HTTP receiver using JSON encoding. Returns the HTTP status."""
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
