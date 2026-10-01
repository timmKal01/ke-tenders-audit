"""End-to-end graph test with a scripted model and the real MCP servers."""

import asyncio
import json
import uuid

from ke_tenders_audit import agent
from ke_tenders_audit.audit_log import AuditLog
from ke_tenders_audit.cases import CASES_DIR, load_flags
from scripted_llm import ScriptedLLM, call

KILIFI_OCID = "ocds-5whusi-290427-KCG/WSNRM/2122735/2025/2026."


def _script(case_id):
    return [
        "1. Search Kilifi awards. 2. Run red flag checks. 3. File flags. 4. Draft report.",
        call("search_awards", {"buyer": "Kilifi Countyy"}, "c1"),           # typo on purpose
        call("check_red_flags", {"buyer": "Kilifi County"}, "c2"),
        call("file_flag", {"case_id": case_id, "ocid": KILIFI_OCID, "flag_type": "just_below_round_amount",
                           "finding": "The award sits just below KES 5,000,000.",
                           "evidence": "KES 4,999,993.25, within 3% of KES 5,000,000.",
                           "approved_by": "agent"}, "c3"),                    # model tries to approve itself
        call("draft_report", {"case_id": case_id, "title": "Kilifi review", "summary": "One pattern found.",
                              "approved_by": ""}, "c4"),
        "Checked Kilifi County awards. Filed one flag. Bid documents are not published, so bids were not checked.",
    ]


def test_full_run_with_human_gate():
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    thread = f"test-{case_id}"
    llm = ScriptedLLM(script=_script(case_id))

    first = asyncio.run(agent.start("Review Kilifi County awards", case_id, thread, llm=llm))
    assert first["status"] == "needs_approval"
    assert first["proposals"][0]["tool"] == "file_flag"

    second = asyncio.run(agent.resume(thread, {"reviewer": "Jane Wanjiku", "approved": ["c3"]}, llm=llm))
    assert second["status"] == "needs_approval"
    assert second["proposals"][0]["tool"] == "draft_report"

    final = asyncio.run(agent.resume(thread, {"reviewer": "Jane Wanjiku", "approved": ["c4"]}, llm=llm))
    assert final["status"] == "done"

    flags = load_flags(case_id)
    assert len(flags) == 1
    assert flags[0]["approved_by"] == "Jane Wanjiku"      # gate replaced the model's "agent"
    report = (CASES_DIR / case_id / "report.md").read_text(encoding="utf-8")
    assert KILIFI_OCID in report and "—" not in report

    log = AuditLog(thread).entries()
    kinds = [e["kind"] for e in log]
    assert kinds.count("human_decision") == 2
    tool_calls = [e for e in log if e["kind"] == "tool_call"]
    assert {e["tool"] for e in tool_calls} >= {"search_awards", "check_red_flags", "file_flag", "draft_report"}
    # the typo produced a hint and the verifier passed it back to the model
    assert any(e["kind"] == "verify" for e in log)


def test_rejected_flag_is_not_written():
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    thread = f"test-{case_id}"
    llm = ScriptedLLM(script=[
        "1. File one flag.",
        call("file_flag", {"case_id": case_id, "ocid": KILIFI_OCID, "flag_type": "x",
                           "finding": "f", "evidence": "e"}, "r1"),
        "Reviewer rejected the flag, so the case file is empty.",
    ])
    first = asyncio.run(agent.start("File a flag", case_id, thread, llm=llm))
    assert first["status"] == "needs_approval"
    final = asyncio.run(agent.resume(thread, {"reviewer": "Jane Wanjiku", "approved": [],
                                              "notes": {"r1": "Not enough evidence"}}, llm=llm))
    assert final["status"] == "done"
    assert load_flags(case_id) == []
    rejection = [m for m in final["messages"] if getattr(m, "tool_call_id", None) == "r1"][0]
    assert json.loads(rejection.content)["status"] == "rejected_by_reviewer"
