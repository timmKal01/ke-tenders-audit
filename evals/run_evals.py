"""Run the eval tasks against the configured model and grade each run.

    python evals/run_evals.py --round 2 --runs 2
    python evals/run_evals.py --round 2 --runs 3 --tasks T01_threshold T06_price_outlier

Results are appended to evals/results/<model>.jsonl, one line per run.
Already-finished runs are skipped, so after a daily rate limit you simply
run the same command again the next day.

Approvals are given by "Eval Harness (automated)". Every approval is still
logged, and the graders check that writes went through the human gate.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ke_tenders_audit import agent  # noqa: E402
from ke_tenders_audit.audit_log import AuditLog  # noqa: E402
from ke_tenders_audit.cases import CASES_DIR, load_flags  # noqa: E402
from tasks import TASKS  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"
REVIEWER = "Eval Harness (automated)"


class DailyLimitReached(Exception):
    pass


def root_error(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


async def run_once(task: dict, run_no: int) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", task["id"].lower())
    case_id = f"eval-{slug}-r{run_no}-{uuid.uuid4().hex[:4]}"
    thread = case_id
    started = time.time()
    approvals = 0
    result = await agent.start(task["question"], case_id, thread)
    while result["status"] == "needs_approval":
        approvals += 1
        result = await agent.resume(thread, {"reviewer": REVIEWER, "approved": [p["id"] for p in result["proposals"]]})
    log = AuditLog(thread).entries()
    report_path = CASES_DIR / case_id / "report.md"
    return {
        "case_id": case_id,
        "summary": result.get("summary", ""),
        "flags": load_flags(case_id),
        "report": report_path.read_text(encoding="utf-8") if report_path.exists() else "",
        "tool_calls": [{"tool": e["tool"], "args": e["args"]} for e in log if e["kind"] == "tool_call"],
        "events": {k: sum(1 for e in log if e["kind"] == k)
                   for k in ("verify", "grounding_refused", "blocked_repeat", "retry")},
        "approvals_requested": approvals,
        "reviewer": REVIEWER,
        "usage": result.get("usage", {}),
        "seconds": round(time.time() - started, 1),
    }


def grade(task: dict, run: dict) -> list[dict]:
    out = []
    for i, grader in enumerate(task["graders"]):
        try:
            ok, note = grader(run)
        except Exception as e:  # a grader crash counts as a failure, with the reason
            ok, note = False, f"grader error: {e}"
        out.append({"check": i + 1, "pass": bool(ok), "note": note})
    return out


def done_runs(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {(r["task"], r["run"]) for r in rows if r["status"] in ("graded", "error")}


async def main(runs: int, only: list[str], round_no: int) -> None:
    model = os.getenv("KTA_LLM_MODEL", "unknown")
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / (re.sub(r"[^a-zA-Z0-9.]+", "_", model) + f"-round{round_no}.jsonl")
    finished = done_runs(path)
    todo = [(t, n) for n in range(1, runs + 1) for t in TASKS
            if (not only or t["id"] in only) and (t["id"], n) not in finished]
    print(f"model {model}: {len(todo)} runs to do, results in {path}")

    for task, n in todo:
        print(f"\n== {task['id']} run {n}: {task['question']}")
        row = {"task": task["id"], "run": n, "model": model, "question": task["question"]}
        try:
            run = await run_once(task, n)
        except Exception as e:
            err = root_error(e)
            text = str(err)
            if "per day" in text or "TPD" in text:
                print("Daily token limit reached. Progress is saved; run the same command tomorrow.")
                return
            row.update(status="error", error=f"{type(err).__name__}: {text[:500]}")
            print("   ERROR", row["error"][:200])
        else:
            checks = grade(task, run)
            row.update(status="graded", passed=all(c["pass"] for c in checks), checks=checks,
                       flags=[(f["flag_type"], f["ocid"]) for f in run["flags"]], summary=run["summary"],
                       events=run["events"], approvals=run["approvals_requested"],
                       tool_calls=len(run["tool_calls"]), usage=run["usage"], seconds=run["seconds"],
                       case_id=run["case_id"])
            print(f"   {'PASS' if row['passed'] else 'FAIL'}  {run['seconds']}s  tokens "
                  f"{run['usage'].get('tokens_in', 0)}+{run['usage'].get('tokens_out', 0)}")
            for c in checks:
                if not c["pass"]:
                    print(f"     x check {c['check']}: {c['note']}")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--tasks", nargs="*", default=[])
    parser.add_argument("--round", type=int, default=2, help="results go to <model>-round<N>.jsonl")
    args = parser.parse_args()
    asyncio.run(main(args.runs, args.tasks, args.round))
