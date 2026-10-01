"""ke-tenders-audit MCP server.

Exposes the audit checks and the case file as MCP tools, and the cleaned
OCDS records as MCP resources. Run it with `ke-tenders-mcp` (stdio).
"""

import json
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP

from . import cases, checks
from .data import get_connection, releases_by_ocid

mcp = FastMCP(
    "ke-tenders-audit",
    instructions=(
        "Tools for reviewing Kenyan public procurement awards (PPRA OCDS data). "
        "Read tools find patterns. Write tools need the name of the human who approved them. "
        "Never award a tender or declare wrongdoing: describe the pattern and cite the ocid."
    ),
)


def _known_ocids() -> set[str]:
    return {r[0] for r in get_connection().execute("SELECT ocid FROM tenders").fetchall()}


def _out(result: dict) -> str:
    """Compact JSON: the free model tier counts every token, so no indentation."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


@mcp.tool()
def search_awards(buyer: str | None = None, item: str | None = None, supplier: str | None = None,
                  category: str | None = None, method: str | None = None,
                  date_from: str | None = None, date_to: str | None = None, limit: int = 10) -> str:
    """Find published awards. All filters optional.

    buyer: part of the buyer name. item: word from the title or item group. supplier: company name.
    category: goods|services|works. method: open|selective|direct. date_from/date_to: YYYY-MM-DD signing date.
    If nothing matches, explains why and suggests close buyer names.
    """
    return _out(checks.search_awards(get_connection(), buyer, item, supplier, category, method,
                                     date_from, date_to, max(1, min(limit, 30))))


@mcp.tool()
def price_benchmark(ocid: str) -> str:
    """Compare one tender's award value with similar awards (same item group and category).

    Returns median, quartiles, ratio to median, outlier yes/no. Says so when under 5 comparables.
    """
    return _out(checks.price_benchmark(get_connection(), ocid))


@mcp.tool()
def supplier_profile(name: str) -> str:
    """Profile a supplier company: bids listed, awards won, win rate, buyers, single-bidder wins.

    Uses fuzzy matching on the company name. Company records only, never personal details.
    """
    return _out(checks.supplier_profile(get_connection(), name))


@mcp.tool()
def check_red_flags(ocid: str | None = None, buyer: str | None = None) -> str:
    """Integrity checks for one tender (ocid) or all awards of a buyer.

    Single bidder, direct method, signed before tender closed, just below a round amount,
    repeat winner at the same buyer. Data problems are listed separately and are not flags.
    """
    return _out(checks.check_red_flags(get_connection(), ocid, buyer))


@mcp.tool()
def file_flag(case_id: str, ocid: str, flag_type: str, finding: str, evidence: str, approved_by: str = "") -> str:
    """WRITE, needs human approval. Add one flag to the case file.

    finding: the pattern in one plain sentence. evidence: the numbers behind it. Refused if the ocid
    is unknown or the wording decides the award or declares wrongdoing.
    """
    try:
        record = None
        if ocid in _known_ocids():
            con = get_connection()
            record = checks.record_facts(con, ocid)
            wrong = checks.suppliers_mentioned(con, f"{finding} {evidence}") - record["_keys"]
            if wrong:
                raise cases.CaseError(
                    f"The flag names {sorted(wrong)}, but on this ocid the winners are {record['winners']} "
                    f"and {record['bidders']} bidder(s) are listed. Check which record you mean.")
            record = {k: v for k, v in record.items() if not k.startswith("_")}
        return _out(cases.file_flag(case_id, ocid, flag_type, finding, evidence, approved_by, _known_ocids(), record))
    except cases.CaseError as e:
        return _out({"status": "refused", "reason": str(e)})


@mcp.tool()
def draft_report(case_id: str, title: str, summary: str, approved_by: str = "") -> str:
    """WRITE. Build the committee report (report.md) from the approved flags in a case.

    Needs a named human approver. Each finding in the report cites its OCDS record.
    """
    try:
        buyers = [f.get("record", {}).get("buyer") for f in cases.load_flags(case_id)]
        totals = checks.check_totals(get_connection(), buyers)
        return _out(cases.draft_report(case_id, title, summary, approved_by, totals))
    except cases.CaseError as e:
        return _out({"status": "refused", "reason": str(e)})


@mcp.resource("ocds://release/{ocid}", mime_type="application/json")
def release(ocid: str) -> str:
    """The cleaned OCDS release behind a finding. The ocid must be URL-encoded."""
    record = releases_by_ocid().get(unquote(ocid))
    if record is None:
        return json.dumps({"error": f"No release with ocid {unquote(ocid)}"})
    return json.dumps(record, ensure_ascii=False)


@mcp.resource("ocds://data-quality-notes", mime_type="text/markdown")
def data_quality_notes() -> str:
    """Known problems in the PPRA feed and how the tools handle them."""
    con = get_connection()
    counts = con.execute("SELECT issue, count(*) FROM data_issues GROUP BY 1 ORDER BY 2 DESC").fetchall()
    lines = ["# Data quality notes (PPRA OCDS)", ""]
    lines += [f"- {issue}: {n} records" for issue, n in counts]
    lines += [
        "- tender startDate is the export time, not a real date: dropped during cleaning.",
        "- personal contact details, and emails or phone numbers typed into company names: removed.",
        "- supplier IDs are portal-internal, so suppliers are matched on normalised names.",
        "- only awarded tenders list their bidders.",
    ]
    return "\n".join(lines)


def main() -> None:
    get_connection()  # build the tables before the first request
    mcp.run()


if __name__ == "__main__":
    main()
