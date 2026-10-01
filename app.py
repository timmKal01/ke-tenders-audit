"""Ke-Tenders Audit review screen.

Run: streamlit run app.py
The reviewer asks a question, watches the agent's tool calls, approves or
rejects each proposed write, and downloads the sourced report.
"""

import asyncio
import json
import re
import os
import uuid
from urllib.parse import quote

import streamlit as st

from ke_tenders_audit import agent
from ke_tenders_audit.audit_log import AuditLog
from ke_tenders_audit.cases import CASES_DIR
from ke_tenders_audit import checks
from ke_tenders_audit.data import get_connection, releases_by_ocid

st.set_page_config(page_title="Ke-Tenders Audit", page_icon="⚖️", layout="wide")

EXAMPLES = [
    "Review all published awards by PC Kinyanjui Technical Training Institute.",
    "Check the Judiciary's awards for prices far above similar awards.",
    "Profile the supplier Silow Smarte Investments and check its wins for patterns.",
    "Review Kilifi County Government's awards.",
]

ss = st.session_state
ss.setdefault("thread_id", None)
ss.setdefault("result", None)
ss.setdefault("error", None)


def run(coro):
    """Run one agent step. On failure keep the run, so 'Retry last step' can continue it."""
    try:
        ss.error = None
        return asyncio.run(coro)
    except Exception as e:  # shown to the reviewer instead of a traceback
        ss.error = describe_error(e)
        return ss.result


def describe_error(e: BaseException) -> str:
    """MCP and asyncio wrap errors in groups; show the reviewer the real one."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    text = str(e)
    if "rate_limit" in text or "Rate limit" in text:
        wait = re.search(r"try again in ([\w.]+)", text)
        scope = "per day" if "per day" in text else "per minute"
        return (f"The model provider's free-tier limit ({scope}) is used up. "
                f"Try again in {wait.group(1).rstrip('.') if wait else 'a while'}, then press Retry last step.\n\n{text}")
    return f"{type(e).__name__}: {text}"


def tool_timeline(thread_id: str):
    for e in AuditLog(thread_id).entries():
        if e["kind"] == "tool_call":
            label = f"🔧 `{e['server']}.{e['tool']}` ({e['seconds']}s)"
            with st.expander(label):
                st.caption("Input")
                st.json(e["args"])
                st.caption("Output")
                st.code(e.get("output") or e.get("error", ""), language="json")
        elif e["kind"] == "verify":
            st.warning("Verifier: " + "; ".join(e["problems"]))
        elif e["kind"] in ("grounding_refused", "blocked_repeat"):
            st.error(f"Refused before review: {e['reason']}")
        elif e["kind"] == "human_decision":
            icon = {"approved": "✅", "changes_requested": "✏️"}.get(e["outcome"], "❌")
            st.info(f"{icon} {e['reviewer']} {e['outcome']} `{e['tool']}`" + (f": {e['note']}" if e["note"] else ""))


def show_facts(ocid: str, claim: str):
    """Put the record's own facts beside the model's claim, and warn on a mismatch."""
    con = get_connection()
    facts = checks.record_facts(con, ocid)
    if not facts["buyer"]:
        st.error("This ocid is not in the data.")
        return
    amounts = ", ".join(f"KES {a:,.0f}" if a else "KES 0 (data gap)" for a in facts["amounts_kes"]) or "none"
    st.markdown(f"**Record says:** \"{facts['title']}\" · winner {', '.join(facts['winners']) or 'none'} · amount {amounts} · "
                f"{facts['bidders']} bidder(s) listed · method {facts['method']} · buyer {facts['buyer']}")
    wrong = checks.suppliers_mentioned(con, claim) - facts["_keys"]
    if wrong:
        st.error(f"Mismatch: the flag names {', '.join(sorted(wrong))}, who is not a bidder or winner on this record.")


def show_record(ocid: str):
    record = releases_by_ocid().get(ocid)
    if record:
        with st.expander(f"Source record {ocid}"):
            st.json(record)
    else:
        st.error(f"ocid {ocid} not found in the data")


with st.sidebar:
    st.header("Reviewer")
    reviewer = st.text_input("Your full name", help="Recorded on every approval in the case file and log.")
    case_id = st.text_input("Case id", value="review-" + uuid.uuid4().hex[:6] if not ss.thread_id else ss.get("case_id", ""))
    st.divider()
    st.caption(f"Model: `{os.getenv('KTA_LLM_MODEL', 'qwen3:8b')}`")
    st.caption(f"Endpoint: `{os.getenv('KTA_LLM_BASE_URL', 'http://localhost:11434/v1')}`")
    st.caption("Data: Kenya PPRA OCDS, FY2026/27, cleaned of personal details.")
    if st.button("New review"):
        ss.thread_id, ss.result = None, None
        st.rerun()

st.title("Ke-Tenders Audit")
st.write("Prepares a sourced review file on public procurement awards for the evaluation committee. "
         "It flags patterns. It never awards a tender. Every write needs your approval.")

if ss.thread_id is None:
    question = st.text_area("What should the agent review?", value=EXAMPLES[0], height=80)
    cols = st.columns(len(EXAMPLES))
    for col, ex in zip(cols, EXAMPLES):
        col.caption(ex)
    if st.button("Start review", type="primary", disabled=not reviewer.strip()):
        ss.thread_id = f"{case_id}-{uuid.uuid4().hex[:6]}"
        ss.case_id = case_id
        with st.spinner("Planning and running checks..."):
            ss.result = run(agent.start(question, case_id, ss.thread_id))
        st.rerun()
    if not reviewer.strip():
        st.caption("Enter your name in the sidebar to start.")
    st.stop()

if ss.error:
    st.error("The last step failed. Your approvals so far are saved. "
             "This is usually a network or rate-limit problem.")
    with st.expander("Error details"):
        st.code(ss.error)
    if st.button("Retry last step", type="primary"):
        with st.spinner("Continuing from the last saved step..."):
            ss.result = run(agent.retry(ss.thread_id))
        st.rerun()
    if ss.result is None:
        st.stop()

result = ss.result
left, right = st.columns([3, 2])

with right:
    st.subheader("Agent activity")
    if result.get("plan"):
        with st.expander("Plan", expanded=True):
            st.markdown(result["plan"])
    tool_timeline(ss.thread_id)

with left:
    if result["status"] == "needs_approval":
        st.subheader("Approval needed")
        st.write("The agent wants to write to the case file. Check each item against its source record.")
        decisions, notes = {}, {}
        for p in result["proposals"]:
            with st.container(border=True):
                args = p["args"]
                st.markdown(f"**{p['tool']}**")
                if p["tool"] == "file_flag":
                    st.markdown(f"- Type: `{args.get('flag_type')}`\n- Finding: {args.get('finding')}\n"
                                f"- Evidence: {args.get('evidence')}\n- Source: `{args.get('ocid')}`")
                    show_facts(args.get("ocid", ""), f"{args.get('finding', '')} {args.get('evidence', '')}")
                    show_record(args.get("ocid", ""))
                else:
                    st.markdown(f"- Title: {args.get('title')}\n- Summary: {args.get('summary')}")
                decisions[p["id"]] = st.radio(
                    "Decision", ["Approve", "Request changes", "Reject"], key=f"d-{p['id']}", horizontal=True,
                    help="Request changes: the agent revises using your note. Reject: it will not be proposed again.")
                notes[p["id"]] = st.text_input("Note (required for Request changes)", key=f"n-{p['id']}")
        missing_note = any(d == "Request changes" and not notes[i].strip() for i, d in decisions.items())
        if missing_note:
            st.caption("Add a note to every item where you request changes.")
        if st.button("Submit decisions", type="primary", disabled=not reviewer.strip() or missing_note):
            decision = {
                "reviewer": reviewer.strip(),
                "approved": [i for i, d in decisions.items() if d == "Approve"],
                "changes": [i for i, d in decisions.items() if d == "Request changes"],
                "notes": {i: n for i, n in notes.items() if n},
            }
            with st.spinner("Continuing the review..."):
                ss.result = run(agent.resume(ss.thread_id, decision))
            st.rerun()
    else:
        st.subheader("Review complete")
        st.markdown(result.get("summary", ""))
        report = CASES_DIR / ss.case_id / "report.md"
        if report.exists():
            text = report.read_text(encoding="utf-8")
            st.download_button("Download report.md", text, file_name=f"{ss.case_id}-report.md")
            with st.container(border=True):
                st.markdown(text)
        else:
            st.info("No report was drafted in this run.")
        usage = result.get("usage", {})
        if usage:
            st.caption(f"{usage['llm_calls']} model calls, {usage['tool_calls']} tool calls, "
                       f"{usage['tokens_in']} tokens in, {usage['tokens_out']} out, "
                       f"cost ${usage['cost_usd']}, model time {usage['llm_seconds']}s")
