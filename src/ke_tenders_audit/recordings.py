"""Save finished real runs so the public demo can replay them without calling a model.

    python -m ke_tenders_audit.recordings save <thread_id> --title "PC Kinyanjui full review"

A recording is a copy of the run's audit log, flags and report, plus metadata.
Nothing is edited: the replay shows exactly what happened, labelled as a recording.
"""

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .audit_log import LOG_DIR
from .cases import CASES_DIR

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS_DIR = ROOT / "demo" / "recordings"
load_dotenv(ROOT / ".env")


def save(thread_id: str, title: str) -> Path:
    log_path = LOG_DIR / f"{thread_id}.jsonl"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    started = next(e for e in entries if e["kind"] == "run_started")
    case_id = started["case_id"]
    calls = [e for e in entries if e["kind"] == "llm_call"]
    name = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    folder = RECORDINGS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(log_path, folder / "log.jsonl")
    for f in ("flags.json", "report.md"):
        if (CASES_DIR / case_id / f).exists():
            shutil.copy(CASES_DIR / case_id / f, folder / f)
    meta = {
        "title": title,
        "question": started["question"],
        "case_id": case_id,
        "model": started.get("model") or os.getenv("KTA_LLM_MODEL", "unknown"),
        "ran_at": entries[0]["ts"],
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reviewers": sorted({e["reviewer"] for e in entries if e["kind"] == "human_decision"}),
        "llm_calls": len(calls),
        "tool_calls": sum(1 for e in entries if e["kind"] == "tool_call"),
        "tokens_in": sum(e["tokens_in"] for e in calls),
        "tokens_out": sum(e["tokens_out"] for e in calls),
        "cost_usd": round(sum(e["cost_usd"] for e in calls), 6),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return folder


def list_recordings() -> list[dict]:
    out = []
    for meta in sorted(RECORDINGS_DIR.glob("*/meta.json")):
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["folder"] = meta.parent
        out.append(data)
    return out


def load(folder: Path) -> dict:
    read = lambda f: (folder / f).read_text(encoding="utf-8") if (folder / f).exists() else ""  # noqa: E731
    return {
        "meta": json.loads(read("meta.json")),
        "entries": [json.loads(line) for line in read("log.jsonl").splitlines() if line],
        "flags": json.loads(read("flags.json") or "[]"),
        "report": read("report.md"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("thread_id")
    s.add_argument("--title", required=True)
    args = parser.parse_args()
    print("Saved", save(args.thread_id, args.title))


if __name__ == "__main__":
    main()
