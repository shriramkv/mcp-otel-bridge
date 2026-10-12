"""Command-line interface. Exit codes: 0 ok, 1 export or validation failed, 2 usage or input error."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import demo as demo_data
from . import otlp, proxy, sources
from .view import stats, tree


def _headers(pairs):
    out = {}
    for p in pairs or []:
        if "=" in p and ":" not in p.split("=", 1)[0]:
            k, v = p.split("=", 1)
        elif ":" in p:
            k, v = p.split(":", 1)
        else:
            raise ValueError("--header expects 'Name: value' or name=value, got %r" % p)
        out[k.strip()] = v.strip()
    env = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    if env:
        for item in env.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def _endpoint(args):
    return args.endpoint or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or \
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")


def _export(spans, args, label) -> int:
    doc = otlp.payload(spans, args.service_name)
    problems = otlp.validate(doc)
    if problems:
        for p in problems:
            print("invalid: " + p, file=sys.stderr)
        return 1
    code = 0
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2 if args.pretty else None)
            fh.write("\n")
    endpoint = _endpoint(args)
    if endpoint:
        status = otlp.post(endpoint, doc, _headers(args.header))
        ok = 200 <= status < 300
        print("%s: exported %d span(s) to %s -> HTTP %d" % (label, len(spans), endpoint, status), file=sys.stderr)
        code = 0 if ok else 1
    if not args.output and not endpoint:
        json.dump(doc, sys.stdout, indent=2 if args.pretty else None)
        sys.stdout.write("\n")
    if getattr(args, "tree", False):
        print(tree(doc), file=sys.stderr)
    s = stats(doc)
    print("%s: %d span(s), %d tool call(s), %d error(s)" % (label, s["spans"], s["tool_calls"], s["errors"]),
          file=sys.stderr)
    if getattr(args, "fail_on_error", False) and s["errors"]:
        return 1
    return code


def cmd_convert(args) -> int:
    ids = otlp.IdGen(args.seed)
    fmt, spans = sources.convert(args.file, args.source, ids, args.capture_content, args.session_id)
    return _export(spans, args, "%s (%s)" % (os.path.basename(args.file), fmt))


def cmd_proxy(args) -> int:
    cmd = [c for c in args.command if c != "--"]
    if not cmd:
        print("error: give the server command after --", file=sys.stderr)
        return 2
    exit_code, records = proxy.run(cmd)
    rows = proxy.as_jsonl_records(records)
    if args.record:
        with open(args.record, "w", encoding="utf-8") as fh:
            for _, r in rows:
                fh.write(json.dumps(r) + "\n")
    if not rows:
        print("proxy: no JSON-RPC traffic seen; nothing to export", file=sys.stderr)
        return exit_code
    spans = sources.jsonrpc_spans(rows, otlp.IdGen(args.seed), rows[0][1]["ts"], args.capture_content,
                                  args.session_id, source="proxy")
    if not args.output and not _endpoint(args):
        args.output = "mcp-trace.otlp.json"
        print("proxy: no --output or endpoint given; writing %s" % args.output, file=sys.stderr)
    rc = _export(spans, args, "proxy")
    return rc or exit_code


def cmd_view(args) -> int:
    with open(args.file, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    problems = otlp.validate(doc)
    print(tree(doc))
    s = stats(doc)
    print("\n%d span(s) in %d trace(s), %d error(s), %d tool call(s)" % (
        s["spans"], s["traces"], s["errors"], s["tool_calls"]))
    for name, n in list(s["tools"].items())[:10]:
        print("  %-30s %d" % (name, n))
    if problems:
        print("\ninvalid OTLP:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    return 0


class _Collector(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        ok = self.path == "/v1/traces" and self.headers.get("Content-Type") == "application/json"
        if ok:
            type(self).received.append(json.loads(body))
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def start_collector():
    """A throwaway OTLP/HTTP receiver used by the demo and tests."""
    _Collector.received = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv, _Collector.received


def cmd_demo(args) -> int:
    tmp = tempfile.mkdtemp(prefix="mcp-otel-bridge-demo-")
    paths = demo_data.write_samples(tmp)
    own = None
    endpoint = args.endpoint
    if not endpoint:
        endpoint, own, received = start_collector()
    try:
        for key in ("goose", "replay", "gateway"):
            fmt, spans = sources.convert(paths[key], "auto", otlp.IdGen("demo-" + key))
            doc = otlp.payload(spans, "mcp-otel-bridge-demo")
            print("== %s  (detected: %s)" % (os.path.basename(paths[key]), fmt))
            print(tree(doc))
            status = otlp.post(endpoint, doc)
            print("   -> posted %d spans to %s/v1/traces: HTTP %d\n" % (len(spans), endpoint, status))

        print("== live proxy around the sample stdio server")
        out = io.BytesIO()
        code, records = proxy.run([sys.executable, "-m", "mcp_otel_bridge.sample_server"],
                                  stdin=io.BytesIO(demo_data.client_script_bytes()), stdout=out)
        rows = proxy.as_jsonl_records(records)
        spans = sources.jsonrpc_spans(rows, otlp.IdGen("demo-proxy"), rows[0][1]["ts"], source="proxy")
        doc = otlp.payload(spans, "mcp-otel-bridge-demo")
        print(tree(doc))
        status = otlp.post(endpoint, doc)
        print("   -> posted %d spans to %s/v1/traces: HTTP %d" % (len(spans), endpoint, status))
        if own:
            total = sum(len(list(otlp.iter_spans(d))) for d in received)
            print("\nlocal test collector received %d payload(s), %d span(s)" % (len(received), total))
        print("sample logs kept in %s" % tmp)
        return 0
    finally:
        if own:
            own.shutdown()
            own.server_close()


def build_parser():
    p = argparse.ArgumentParser(prog="mcp-otel-bridge",
                                description="Turn MCP and goose activity into OpenTelemetry traces.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="cmd")

    def export_opts(sp):
        sp.add_argument("--output", "-o", metavar="FILE", help="write OTLP/JSON to FILE")
        sp.add_argument("--endpoint", help="OTLP/HTTP endpoint, e.g. http://localhost:4318 "
                                           "(or env OTEL_EXPORTER_OTLP_ENDPOINT)")
        sp.add_argument("--header", action="append", metavar="'Name: value'",
                        help="header for the endpoint (also read from OTEL_EXPORTER_OTLP_HEADERS)")
        sp.add_argument("--service-name", default=os.environ.get("OTEL_SERVICE_NAME", "mcp-otel-bridge"))
        sp.add_argument("--capture-content", action="store_true",
                        help="include tool arguments and results (secrets redacted, 2 KB cap). Off by default.")
        sp.add_argument("--session-id", help="value for mcp.session.id / gen_ai.conversation.id")
        sp.add_argument("--seed", help="derive IDs from this seed (reproducible output)")
        sp.add_argument("--pretty", action="store_true", help="indent the JSON")
        sp.add_argument("--fail-on-error", action="store_true", help="exit 1 if any span has error status")

    c = sub.add_parser("convert", help="convert a log file to OTLP")
    c.add_argument("file")
    c.add_argument("--from", dest="source", choices=("auto", "goose", "jsonrpc", "audit"), default="auto")
    c.add_argument("--tree", action="store_true", help="print the span tree to stderr")
    export_opts(c)
    c.set_defaults(func=cmd_convert)

    x = sub.add_parser("proxy", help="run an MCP stdio server behind a tracing proxy")
    x.add_argument("--record", metavar="FILE", help="also save the raw session as JSONL")
    x.add_argument("--tree", action="store_true")
    export_opts(x)
    x.add_argument("command", nargs=argparse.REMAINDER, help="-- server command and args")
    x.set_defaults(func=cmd_proxy)

    v = sub.add_parser("view", help="show an OTLP/JSON file as a span tree and validate it")
    v.add_argument("file")
    v.set_defaults(func=cmd_view)

    d = sub.add_parser("demo", help="convert the bundled samples and trace the sample server")
    d.add_argument("--endpoint", help="send to this collector instead of a throwaway local one")
    d.set_defaults(func=cmd_demo)

    ver = sub.add_parser("version")
    ver.set_defaults(func=lambda a: (print(__version__), 0)[1])
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except (sources.SourceError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
