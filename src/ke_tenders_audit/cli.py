"""Run a review from the terminal.

    python -m ke_tenders_audit.cli "Review Kilifi County awards" --reviewer "Jane Wanjiku"

You are asked to approve or reject each write. --auto-approve exists only for
the eval harness and is recorded in the log as an automated reviewer.
"""

import argparse
import asyncio
import json
import uuid

from . import agent


def ask(proposals: list[dict], reviewer: str) -> dict:
    approved, notes = [], {}
    for p in proposals:
        print(f"\n--- {p['tool']} ---")
        print(json.dumps(p["args"], indent=2, ensure_ascii=False))
        answer = input("Approve? [y/n]: ").strip().lower()
        if answer == "y":
            approved.append(p["id"])
        else:
            notes[p["id"]] = input("Reason for rejecting: ").strip() or "rejected"
    return {"reviewer": reviewer, "approved": approved, "notes": notes}


async def review(question: str, case_id: str, reviewer: str, auto_approve: bool) -> dict:
    thread = f"{case_id}-{uuid.uuid4().hex[:6]}"
    result = await agent.start(question, case_id, thread)
    while result["status"] == "needs_approval":
        if auto_approve:
            decision = {"reviewer": reviewer, "approved": [p["id"] for p in result["proposals"]],
                        "notes": {p["id"]: "automated eval approval" for p in result["proposals"]}}
        else:
            decision = ask(result["proposals"], reviewer)
        result = await agent.resume(thread, decision)
    result["thread_id"] = thread
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ke-Tenders Audit review")
    parser.add_argument("question")
    parser.add_argument("--case", default=f"review-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--reviewer", required=True, help="Full name of the human approving writes")
    parser.add_argument("--auto-approve", action="store_true", help="Eval harness only")
    args = parser.parse_args()
    result = asyncio.run(review(args.question, args.case, args.reviewer, args.auto_approve))
    print("\nPLAN:\n" + (result.get("plan") or ""))
    print("\nSUMMARY:\n" + result.get("summary", ""))
    print("\nUSAGE:", result.get("usage"), "| log: logs/" + result["thread_id"] + ".jsonl")


if __name__ == "__main__":
    main()
