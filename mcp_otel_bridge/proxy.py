"""Live mode: a transparent stdio proxy that turns an MCP session into a trace.

    client  <->  mcp-otel-bridge proxy  <->  MCP server (subprocess)

Bytes are forwarded unchanged in both directions. Each line is also timestamped
and, when it parses as JSON, kept for the trace. Nothing but the server's own
output is ever written to stdout; bridge messages go to stderr.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import BinaryIO, List, Optional, Tuple


def _pump(src: BinaryIO, dst: BinaryIO, direction: str, records: list, lock: threading.Lock,
          close_dst: bool) -> None:
    try:
        for line in iter(src.readline, b""):
            ts = time.time_ns()
            try:
                dst.write(line)
                dst.flush()
            except (BrokenPipeError, ValueError, OSError):
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            with lock:
                records.append((ts, direction, msg))
    finally:
        if close_dst:
            try:
                dst.close()
            except OSError:
                pass


def run(cmd: List[str], stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None,
        env=None) -> Tuple[int, List[Tuple[int, str, dict]]]:
    """Run ``cmd`` behind the proxy. Returns (exit_code, [(ts_ns, direction, message)])."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env)
    records: list = []
    lock = threading.Lock()
    t_in = threading.Thread(target=_pump, args=(stdin, proc.stdin, "c2s", records, lock, True), daemon=True)
    t_out = threading.Thread(target=_pump, args=(proc.stdout, stdout, "s2c", records, lock, False), daemon=True)
    t_in.start()
    t_out.start()
    code = None
    try:
        t_out.join()          # server closed stdout: session is over
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    finally:
        for stream in (proc.stdout, proc.stdin):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
    records.sort(key=lambda r: r[0])
    return code, records


def as_jsonl_records(records: List[Tuple[int, str, dict]]) -> List[Tuple[int, dict]]:
    """Shape proxy records like parsed JSONL so sources.jsonrpc_spans can read them."""
    out = []
    for i, (ts, direction, msg) in enumerate(records, start=1):
        if isinstance(msg, list):  # JSON-RPC batch
            out.extend((i, {"ts": ts, "direction": direction, "message": m}) for m in msg if isinstance(m, dict))
        elif isinstance(msg, dict):
            out.append((i, {"ts": ts, "direction": direction, "message": msg}))
    return out
