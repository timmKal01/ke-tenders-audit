# Architecture

![Architecture](docs/architecture.png)

## Agent shape

A LangGraph state graph with six nodes. The model decides what to do; code decides what is allowed.

| Node | Who acts | What it does |
|---|---|---|
| `plan` | model | Writes a 3 to 6 step plan for the request. |
| `agent` | model | Picks up to 3 tool calls per step, or finishes with a plain summary. |
| `tools` | code | Runs the calls through MCP. Errors go back to the model as results, not crashes. |
| `verify` | code | Reads the latest results for errors, refusals, hints and "insufficient comparables" and tells the model plainly, so it adjusts instead of guessing. |
| `gate` | code, then human | Refuses flags whose ocid no tool returned, and writes the reviewer already rejected. Then `interrupt()` pauses for a named human: approve, request changes or reject. Writes the reviewer's name into `approved_by`. |
| `stop` | code | Ends the run after 16 agent steps. |

State is checkpointed in SQLite after every node, so a run survives the pause for approval and can
continue after a network or rate-limit error. Each run has its own JSONL audit log: every model call
(tokens, cost, time), every tool call (inputs, output, time) and every human decision.

Each request is kept under about 7,000 tokens: older tool results are shortened to a character budget,
and `check_red_flags` puts its totals first so they survive shortening.

## MCP servers: built versus borrowed

| Server | Built or borrowed | Why |
|---|---|---|
| `ke-tenders-audit-mcp` | **Built** (official Python SDK, stdio) | The domain logic: OCDS search, integrity checks, price benchmarks, supplier profiles, and the two gated write tools. Nothing existing does this, and the rules that make flags trustworthy (real ocid, right supplier, named approver, no decision language) belong next to the data, not in a prompt. |
| Filesystem MCP server | **Borrowed** (`@modelcontextprotocol/server-filesystem`) | Lets the agent read earlier case files. It is maintained, tested and sandboxed to one folder; writing our own file access would add risk and no value. Only its read tools are given to the agent, so all writes go through our gated tools. |

The tool boundaries are meant to be reusable: `search_awards`, `check_red_flags`, `price_benchmark`
and `supplier_profile` take plain arguments and return compact JSON with the ocid on every finding,
so any MCP client, or another OCDS country, can use them.

## Why the safety rules live in code

Development runs on the real model showed four failures that prompts alone did not stop: an invented
ocid, the wrong supplier named for a real ocid, a rejected report proposed again, and a summary that
left out patterns. Each is now a code check with a test. Details in [EVALS.md](EVALS.md).

## Data flow

PPRA OCDS JSON (tenders.go.ke) is cleaned once by `ingest.py` (personal details removed) into
`data/clean/releases.jsonl`. The MCP server loads it into in-memory DuckDB tables (tenders, awards,
bidders, data_issues) in under a second. Approved flags and reports go to `cases/<case_id>/`.

## Model

Any OpenAI-compatible endpoint serving an open-weights model. Development and evals use Qwen3.8-27B on
Groq's free tier. The same code runs Qwen3 4B locally through Ollama, so procurement data need not leave
the machine.
