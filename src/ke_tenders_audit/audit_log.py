"""Append-only JSONL audit log: every tool call, model call and human decision.

One file per run thread: logs/<thread_id>.jsonl. Each line has a timestamp.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"


class AuditLog:
    def __init__(self, thread_id: str):
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"{thread_id}.jsonl"
        self.thread_id = thread_id
        self.price_in = float(os.getenv("KTA_COST_PER_MTOK_IN", "0"))
        self.price_out = float(os.getenv("KTA_COST_PER_MTOK_OUT", "0"))

    def _write(self, kind: str, data: dict) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def event(self, name: str, data: dict) -> None:
        self._write(name, data)

    async def interceptor(self, request, handler):
        """MCP tool interceptor: logs inputs, output and duration of every tool call."""
        started = time.time()
        try:
            result = await handler(request)
        except Exception as e:
            self._write("tool_call", {"server": request.server_name, "tool": request.name, "args": request.args,
                                      "error": f"{type(e).__name__}: {e}",
                                      "seconds": round(time.time() - started, 3)})
            raise
        self._write("tool_call", {"server": request.server_name, "tool": request.name, "args": request.args,
                                  "output": _preview(result), "seconds": round(time.time() - started, 3)})
        return result

    def llm_call(self, step: str, message, seconds: float) -> None:
        usage = getattr(message, "usage_metadata", None) or {}
        tokens_in, tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        self._write("llm_call", {
            "step": step,
            "tool_calls": [c["name"] for c in getattr(message, "tool_calls", [])],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round((tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000, 6),
            "seconds": round(seconds, 2),
        })

    def decision(self, call: dict, outcome: str, reviewer: str, note: str) -> None:
        self._write("human_decision", {"tool": call["name"], "args": call["args"], "outcome": outcome,
                                       "reviewer": reviewer, "note": note})

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def totals(self) -> dict:
        calls = [e for e in self.entries() if e["kind"] == "llm_call"]
        tools = [e for e in self.entries() if e["kind"] == "tool_call"]
        return {
            "llm_calls": len(calls),
            "tool_calls": len(tools),
            "tokens_in": sum(e["tokens_in"] for e in calls),
            "tokens_out": sum(e["tokens_out"] for e in calls),
            "cost_usd": round(sum(e["cost_usd"] for e in calls), 6),
            "llm_seconds": round(sum(e["seconds"] for e in calls), 1),
        }


def _preview(result, limit: int = 2000) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        content = "\n".join(getattr(b, "text", None) or (b.get("text", "") if isinstance(b, dict) else str(b))
                            for b in content)
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"
