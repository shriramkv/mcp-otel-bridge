"""A tiny stdio MCP server used by the demo and tests.

Tools: ``add`` (works), ``divide`` (returns isError on divide by zero),
``slow`` (sleeps a little). Unknown methods get a JSON-RPC error.
"""

import json
import sys
import time

TOOLS = [
    {"name": "add", "description": "Add two numbers.", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
    {"name": "divide", "description": "Divide a by b.", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
    {"name": "slow", "description": "Wait briefly, then answer.", "inputSchema": {"type": "object"}},
]


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "calc-sample", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "add":
            text, err = str(args.get("a", 0) + args.get("b", 0)), False
        elif name == "divide":
            if not args.get("b"):
                text, err = "division by zero", True
            else:
                text, err = str(args["a"] / args["b"]), False
        elif name == "slow":
            time.sleep(0.05)
            text, err = "done", False
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool %s" % name}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
