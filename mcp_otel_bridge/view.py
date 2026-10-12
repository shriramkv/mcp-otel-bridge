"""Human-readable view of an OTLP/JSON payload: a span tree plus totals."""

from __future__ import annotations

from typing import Dict, List

from .otlp import iter_spans

_STATUS = {0: "", 1: "ok", 2: "ERROR"}


def _attr(sp: dict, key: str):
    for a in sp.get("attributes", []):
        if a.get("key") == key:
            v = a.get("value", {})
            return next(iter(v.values()), None)
    return None


def tree(doc: dict, max_children: int = 50) -> str:
    spans = list(iter_spans(doc))
    by_parent: Dict[str, List[dict]] = {}
    ids = {s["spanId"] for s in spans}
    roots = []
    for s in spans:
        p = s.get("parentSpanId")
        if p and p in ids:
            by_parent.setdefault(p, []).append(s)
        else:
            roots.append(s)
    lines = []

    def dur(s):
        return (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6

    def walk(s, prefix, last, depth):
        status = _STATUS.get(s.get("status", {}).get("code", 0), "")
        err = _attr(s, "error.type")
        tail = "  %s" % status if status else ""
        if err:
            tail += " (%s)" % err
        branch = "" if depth == 0 else ("`- " if last else "|- ")
        lines.append("%s%s%s  %.1f ms%s" % (prefix, branch, s["name"], dur(s), tail))
        kids = sorted(by_parent.get(s["spanId"], []), key=lambda x: int(x["startTimeUnixNano"]))
        shown = kids[:max_children]
        for i, k in enumerate(shown):
            walk(k, prefix + ("" if depth == 0 else ("   " if last else "|  ")), i == len(shown) - 1, depth + 1)
        if len(kids) > max_children:
            lines.append(prefix + "   ... %d more" % (len(kids) - max_children))

    for r in sorted(roots, key=lambda x: int(x["startTimeUnixNano"])):
        walk(r, "", True, 0)
    return "\n".join(lines)


def stats(doc: dict) -> dict:
    spans = list(iter_spans(doc))
    errors = [s for s in spans if s.get("status", {}).get("code") == 2]
    tools: Dict[str, int] = {}
    for s in spans:
        t = _attr(s, "gen_ai.tool.name")
        if t:
            tools[t] = tools.get(t, 0) + 1
    return {
        "spans": len(spans),
        "traces": len({s["traceId"] for s in spans}),
        "errors": len(errors),
        "tool_calls": sum(tools.values()),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
    }
