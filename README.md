# mcp-otel-bridge

**See your MCP and agent activity in the observability stack you already run.**

MCP servers, goose sessions and guardrail gateways all leave logs behind: JSONL files that nobody reads until something breaks. `mcp-otel-bridge` turns them into OpenTelemetry traces, so tool calls show up as spans in Jaeger, Grafana Tempo, Honeycomb, Datadog or any OTLP backend, next to the rest of your services.

It works two ways:

- **Batch:** convert an existing log file (goose session, MCP JSON-RPC recording such as an mcp-replay cassette, or a guardrail audit trail).
- **Live:** run an MCP stdio server behind a transparent proxy and get a trace of the whole session.

Part of the AAIF toolchain. Standard library only, no OpenTelemetry SDK needed. Python 3.9+. MIT.

## Quick start

```bash
pip install git+https://github.com/shriramkv/mcp-otel-bridge
mcp-otel-bridge demo
```

```
== replay-cassette.jsonl  (detected: jsonrpc)
mcp.session weather  610.0 ms  ERROR
|- initialize  12.0 ms  ok
|- tools/list  11.0 ms  ok
|- tools/call forecast  380.0 ms  ok
|- tools/call forecast  30.0 ms  ERROR (tool_error)
`- resources/read weather://alerts  10.0 ms  ERROR (-32002)
```

The demo converts one sample of each log type, traces a live sample server through the proxy, and posts everything to a throwaway local OTLP receiver. Point it at a real one with `--endpoint`.

## Batch: convert logs

```bash
# to a file (OTLP/JSON, loadable by the collector's otlpjsonfile receiver)
mcp-otel-bridge convert ~/.local/share/goose/sessions/abc.jsonl -o trace.json

# straight to a collector over OTLP/HTTP
mcp-otel-bridge convert cassette.jsonl --endpoint http://localhost:4318

# look at a trace without a backend
mcp-otel-bridge view trace.json
```

The format is detected automatically; force it with `--from goose|jsonrpc|audit`.

## Live: proxy a server

Put the bridge in front of the server command in your MCP client config:

```json
{
  "mcpServers": {
    "weather": {
      "command": "mcp-otel-bridge",
      "args": ["proxy", "--endpoint", "http://localhost:4318", "--", "python", "-m", "weather_server"]
    }
  }
}
```

Bytes pass through unchanged in both directions. Only the server's own output ever reaches stdout; bridge messages go to stderr. The trace is exported when the session ends. `--record session.jsonl` also keeps the raw session, which `convert` can replay later.

## Configuration

| Option | Environment variable | |
|---|---|---|
| `--endpoint` | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP receiver; `/v1/traces` is appended if missing |
| `--header 'k: v'` | `OTEL_EXPORTER_OTLP_HEADERS` | e.g. API keys for hosted backends |
| `--service-name` | `OTEL_SERVICE_NAME` | default `mcp-otel-bridge` |
| `--capture-content` | | record tool arguments and results (off by default) |
| `--session-id` | | sets `mcp.session.id` / `gen_ai.conversation.id` |
| `--seed` | | reproducible span IDs, for golden-file tests |
| `--fail-on-error` | | exit 1 if any span has error status (CI) |

## What the spans look like

Attribute names follow the OpenTelemetry **GenAI** and **MCP** semantic conventions. Those conventions are still in development status and now live in the [semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai) repository, so every name is kept in one file, `mcp_otel_bridge/semconv.py`, to make updates a one-line change. Anything not covered by the conventions uses the `mcp_bridge.` prefix.

**MCP JSON-RPC (cassettes and the live proxy)**: one root `mcp.session <server>` span, one child per request named `{method} {target}` (for example `tools/call forecast`), kind CLIENT, or SERVER for server-initiated requests such as sampling.

| Attribute | Value |
|---|---|
| `mcp.method.name` | `tools/call`, `resources/read`, ... |
| `gen_ai.operation.name` | `execute_tool` on tool calls |
| `gen_ai.tool.name`, `gen_ai.prompt.name`, `mcp.resource.uri` | the call's target |
| `jsonrpc.request.id`, `mcp.protocol.version`, `network.transport` | protocol details (`pipe` for stdio) |
| `error.type` | the JSON-RPC error code, `tool_error` when a result has `isError: true`, `no_response` when a request was never answered |

Notifications become events on the root span.

**goose sessions**: a root `invoke_agent goose` span with `gen_ai.conversation.id` and token usage when the session header has it, and one `execute_tool <name>` span per tool call, timed from request to response. The goose extension (the part before `__`) is kept as `mcp_bridge.goose.extension`.

**Guardrail audit logs**: one `tools/call <tool>` span per decision. Enforced denials are errors with `error.type=policy_denied`; monitor-mode would-be blocks stay OK and carry a `guardrail.would_block` event.

## Recognised log fields

Readers are lenient, because each tool writes a slightly different envelope.

- **JSON-RPC:** either bare JSON-RPC objects per line, or an envelope with the message under `message`, `msg`, `payload`, `data`, `frame` or `json` (object or JSON string), a timestamp under `ts`, `timestamp`, `time`, `t`, `at` or `created`, and an optional direction under `direction`, `dir`, `from`, `source`, `stream` or `side` (`client->server`, `c2s`, `server->client`, `s2c`, ...). Without a direction, requests are assumed to come from the client.
- **Audit:** `tool`/`tool_name`/`name`, `decision`/`verdict`/`action`, optional `reason`, `enforced`, `id`, `duration_ms` and a timestamp as above.
- **Timestamps** may be seconds, milliseconds, microseconds or nanoseconds (detected by size) or ISO 8601. Missing ones are filled in 1 ms after the previous entry.

## Privacy

By default no tool arguments, results or message text are exported, only names, timings and outcomes. With `--capture-content`, content is passed through secret redaction (AWS keys, GitHub and Slack tokens, `sk-` keys, JWTs, bearer tokens) and capped at 2 KB per attribute.

## Where it fits

| Tool | Output |
|---|---|
| mcp-replay | records sessions as cassettes |
| mcp-guardrail-gateway | enforces policy, writes an audit trail |
| goose-session-report | summarises a goose session for people |
| **mcp-otel-bridge** | **sends all of the above to your tracing backend** |

## Development

```bash
python -m unittest discover -s tests -t . -v
```

## License

MIT
