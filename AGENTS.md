# AGENTS.md

Guidance for AI coding agents working on mcp-otel-bridge.

## Project
- Pure Python 3.9+, standard library only. Do not add runtime dependencies (no opentelemetry-sdk); the OTLP/JSON encoding is written by hand in `mcp_otel_bridge/otlp.py`.
- Every attribute name lives in `mcp_otel_bridge/semconv.py`. Names outside the OpenTelemetry conventions use the `mcp_bridge.` prefix.

## Workflow
- Always run `python -m unittest discover -s tests -t .` before committing.
- Run `python -m mcp_otel_bridge demo` after changing a source reader or the OTLP encoder.
- Never run `git push --force`.

## Boundaries
- In proxy mode, never write anything to stdout except the server's own bytes. Bridge messages go to stderr.
- Tool arguments and results are recorded only with `--capture-content`, and always through `redact()`.
- Never print or commit secrets, tokens or API keys.
- Do not modify `LICENSE`.
