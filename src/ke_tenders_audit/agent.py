"""The Ke-Tenders Audit agent: a LangGraph graph over two MCP servers.

    plan -> agent -> (read tools) -> tools -> verify -> agent ...
                  -> (write tools) -> human gate -> tools -> verify -> agent ...
                  -> (no tool calls) -> done

The human gate uses LangGraph interrupt(): the run pauses, a named reviewer
approves or rejects each proposed write, and the run resumes from the
checkpoint. The reviewer's name is written into approved_by by the gate,
never by the model.
"""

import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from .audit_log import AuditLog

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "cases"
CHECKPOINTS = ROOT / "logs" / "checkpoints.sqlite"

WRITE_TOOLS = {"file_flag", "draft_report"}
BORROWED_READ_TOOLS = {"read_text_file", "list_directory", "search_files"}
MAX_AGENT_STEPS = 16

load_dotenv(ROOT / ".env")

PLAN_PROMPT = """You plan reviews of Kenyan public procurement awards for a human evaluation committee.
Write a short numbered plan (3 to 6 steps) for the request below, using these tools:
search_awards, check_red_flags, price_benchmark, supplier_profile, file_flag, draft_report,
and read_text_file / list_directory / search_files for earlier case files.
Plain sentences only. No em dashes. Do not call tools now."""

AGENT_PROMPT = """You are Ke-Tenders Audit. You prepare a sourced review file for a procurement
committee, then stop. You never award a tender, never recommend who should win, and never declare
fraud or guilt. You describe patterns and cite the OCDS ocid for every one.

Case id: {case_id}

Plan:
{plan}

How to work:
- Use tools to get facts. Never invent numbers or ocids.
- If a tool returns an error, a hint or no results, read it and adjust: fix the name, broaden the
  filter, or explain that the data cannot answer the question.
- Treat data problems (zero amounts, missing fields) as data gaps, not as flags.
- When you find a pattern worth the committee's attention, call file_flag with case_id "{case_id}",
  the ocid, a flag_type, a one-sentence finding and the evidence numbers. Leave approved_by empty:
  a human reviewer fills it in.
- File at most 5 flags, the most important ones. When done, call draft_report once.
- Then reply with a short plain summary of what you checked, what you flagged and what you could not check.
Write plain sentences. Never use em dashes."""


class State(TypedDict):
    question: str
    case_id: str
    plan: str
    messages: Annotated[list, add_messages]
    steps: int


def make_llm():
    return ChatOpenAI(
        base_url=os.getenv("KTA_LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.getenv("KTA_LLM_API_KEY", "ollama"),
        model=os.getenv("KTA_LLM_MODEL", "qwen3:8b"),
        temperature=float(os.getenv("KTA_LLM_TEMPERATURE", "0")),
        timeout=180,
    )


def mcp_connections() -> dict:
    npx = shutil.which("npx")
    connections = {
        "audit": {"transport": "stdio", "command": sys.executable, "args": ["-m", "ke_tenders_audit.server"]},
    }
    if npx:
        CASES_DIR.mkdir(exist_ok=True)
        connections["files"] = {
            "transport": "stdio",
            "command": npx,
            "args": ["-y", "@modelcontextprotocol/server-filesystem", str(CASES_DIR)],
        }
    return connections


def _last_ai(messages) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


def _pending_calls(messages) -> list[dict]:
    """Tool calls in the last AI message that have no ToolMessage answer yet."""
    ai = _last_ai(messages)
    if ai is None:
        return []
    idx = messages.index(ai)
    answered = {m.tool_call_id for m in messages[idx + 1:] if isinstance(m, ToolMessage)}
    return [c for c in ai.tool_calls if c["id"] not in answered]


def build_graph(llm, tools, log: AuditLog, checkpointer=None):
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    async def plan(state: State):
        started = time.time()
        reply = await llm.ainvoke([SystemMessage(PLAN_PROMPT), HumanMessage(state["question"])])
        log.llm_call("plan", reply, time.time() - started)
        return {"plan": _strip_thinking(reply.content), "messages": [HumanMessage(state["question"])], "steps": 0}

    async def agent(state: State):
        started = time.time()
        system = SystemMessage(AGENT_PROMPT.format(case_id=state["case_id"], plan=state["plan"]))
        reply = await llm_with_tools.ainvoke([system, *state["messages"]])
        log.llm_call("agent", reply, time.time() - started)
        return {"messages": [reply], "steps": state["steps"] + 1}

    def route_after_agent(state: State):
        calls = _pending_calls(state["messages"])
        if not calls:
            return END
        if state["steps"] >= MAX_AGENT_STEPS:
            return "stop"
        return "gate" if any(c["name"] in WRITE_TOOLS for c in calls) else "tools"

    def gate(state: State):
        ai = _last_ai(state["messages"])
        writes = [c for c in _pending_calls(state["messages"]) if c["name"] in WRITE_TOOLS]
        decision = interrupt({
            "type": "approval_needed",
            "proposals": [{"id": c["id"], "tool": c["name"], "args": c["args"]} for c in writes],
        })
        reviewer = (decision.get("reviewer") or "").strip()
        approved = set(decision.get("approved", []))
        notes = decision.get("notes", {})

        new_calls, rejections = [], []
        for c in ai.tool_calls:
            if c["name"] not in WRITE_TOOLS:
                new_calls.append(c)
            elif c["id"] in approved and reviewer:
                new_calls.append({**c, "args": {**c["args"], "approved_by": reviewer}})
            else:
                note = notes.get(c["id"], "no reason given")
                rejections.append(ToolMessage(
                    content=json.dumps({"status": "rejected_by_reviewer", "reviewer": reviewer or "unknown",
                                        "note": note, "instruction": "Do not file this again unless the reviewer asks."}),
                    tool_call_id=c["id"], name=c["name"]))
                log.decision(c, "rejected", reviewer, note)
        for c in new_calls:
            if c["name"] in WRITE_TOOLS:
                log.decision(c, "approved", reviewer, notes.get(c["id"], ""))  # args now carry the reviewer
        updated = AIMessage(content=ai.content, tool_calls=new_calls, id=ai.id)
        return {"messages": [updated, *rejections]}

    async def run_tools(state: State):
        results = []
        for call in _pending_calls(state["messages"]):
            tool = tools_by_name.get(call["name"])
            if tool is None:
                content = json.dumps({"error": f"Unknown tool {call['name']}"})
            else:
                try:
                    content = await tool.ainvoke(call["args"])
                except Exception as e:  # tool errors go back to the model so it can recover
                    content = json.dumps({"error": f"{type(e).__name__}: {e}"})
            results.append(ToolMessage(content=_as_text(content), tool_call_id=call["id"], name=call["name"]))
        return {"messages": results}

    def verify(state: State):
        """Check the latest tool results and tell the model plainly what went wrong."""
        problems = []
        for m in reversed(state["messages"]):
            if not isinstance(m, ToolMessage):
                break
            text = m.content if isinstance(m.content, str) else json.dumps(m.content)
            for marker in ('"error"', '"refused"', '"hint"', "insufficient comparables"):
                if marker in text:
                    problems.append(f"{m.name}: result contains {marker.strip(chr(34))}")
                    break
        if not problems:
            return {}
        log.event("verify", {"problems": problems})
        note = ("Verifier: " + "; ".join(problems) +
                ". Read those results, then adjust your next step or state the limitation. Do not guess.")
        return {"messages": [HumanMessage(note)]}

    def stop(state: State):
        return {"messages": [AIMessage(content="Stopped: step limit reached. The case file holds only approved flags so far.")]}

    graph = StateGraph(State)
    graph.add_node("plan", plan)
    graph.add_node("agent", agent)
    graph.add_node("gate", gate)
    graph.add_node("tools", run_tools)
    graph.add_node("verify", verify)
    graph.add_node("stop", stop)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "agent")
    graph.add_conditional_edges("agent", route_after_agent, ["gate", "tools", "stop", END])
    graph.add_edge("gate", "tools")
    graph.add_edge("tools", "verify")
    graph.add_edge("verify", "agent")
    graph.add_edge("stop", END)
    return graph.compile(checkpointer=checkpointer)


def _as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # MCP adapters return content blocks
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return json.dumps(content, default=str)


def _strip_thinking(text: str) -> str:
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


@asynccontextmanager
async def open_agent(thread_id: str, llm=None):
    """Open MCP sessions, the checkpointer and the audit log for one run or resume."""
    log = AuditLog(thread_id)
    client = MultiServerMCPClient(mcp_connections(), tool_interceptors=[log.interceptor])
    CHECKPOINTS.parent.mkdir(exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTS)) as saver:
        async with client.session("audit") as audit_session:
            tools = await load_mcp_tools(audit_session, tool_interceptors=[log.interceptor], server_name="audit")
            if "files" in client.connections:
                async with client.session("files") as files_session:
                    borrowed = await load_mcp_tools(files_session, tool_interceptors=[log.interceptor],
                                                    server_name="files")
                    tools += [t for t in borrowed if t.name in BORROWED_READ_TOOLS]
                    yield build_graph(llm or make_llm(), tools, log, saver), log
            else:
                yield build_graph(llm or make_llm(), tools, log, saver), log


async def start(question: str, case_id: str, thread_id: str, llm=None) -> dict:
    async with open_agent(thread_id, llm) as (graph, log):
        log.event("run_started", {"question": question, "case_id": case_id})
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
        result = await graph.ainvoke({"question": question, "case_id": case_id}, config)
        return _outcome(result, log)


async def resume(thread_id: str, decision: dict, llm=None) -> dict:
    async with open_agent(thread_id, llm) as (graph, log):
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
        result = await graph.ainvoke(Command(resume=decision), config)
        return _outcome(result, log)


def _outcome(result: dict, log: AuditLog) -> dict:
    pending = result.get("__interrupt__")
    if pending:
        return {"status": "needs_approval", "proposals": pending[0].value["proposals"],
                "plan": result.get("plan"), "messages": result["messages"]}
    final = _last_ai(result["messages"])
    log.event("run_finished", {"summary": final.content if final else ""})
    return {"status": "done", "plan": result.get("plan"), "summary": _strip_thinking(final.content if final else ""),
            "messages": result["messages"], "usage": log.totals()}
