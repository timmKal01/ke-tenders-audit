"""ke-tenders-audit MCP server.

Exposes the audit checks and the case file as MCP tools, and the cleaned
OCDS records as MCP resources. Run it with `ke-tenders-mcp` (stdio).
"""

import json
from urllib.parse import quote, unquote

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


def _resource_uri(ocid: str) -> str:
    return f"ocds://release/{quote(ocid, safe='')}"


@mcp.tool()
def search_awards(buyer: str | None = None, item: str | None = None, supplier: str | None = None,
                  category: str | None = None, method: str | None = None,
                  date_from: str | None = None, date_to: str | None = None, limit: int = 25) -> dict:
    """Find published awards. Filters are optional and combine with AND.

    buyer: part of the buyer name, e.g. 'Kilifi County'.
    item: a word from the tender title or an item group, e.g. 'stationery'.
    supplier: company name (normalised, so 'Ltd' and 'Limited' match).
    category: goods, services or works. method: open, selective or direct.
    date_from / date_to: contract signing date, YYYY-MM-DD.
    If nothing matches, the result explains why and suggests close buyer names.
    """
    return checks.search_awards(get_connection(), buyer, item, supplier, category, method,
                                date_from, date_to, max(1, min(limit, 100)))


@mcp.tool()
def price_benchmark(ocid: str) -> dict:
    """Compare the award value(s) on one tender with similar past awards (same item group and category).

    Returns median, quartiles, ratio to median and whether it is an outlier, plus the comparable ocids.
    Says 'insufficient comparables' rather than guessing when there are fewer than 5.
    """
    return checks.price_benchmark(get_connection(), ocid)


@mcp.tool()
def supplier_profile(name: str) -> dict:
    """Profile a supplier company: bids listed, awards won, win rate, buyers, single-bidder wins.

    Uses fuzzy matching on the company name. Company records only, never personal details.
    """
    return checks.supplier_profile(get_connection(), name)


@mcp.tool()
def check_red_flags(ocid: str | None = None, buyer: str | None = None) -> dict:
    """Run the integrity checks on one tender (ocid) or all awards of a buyer.

    Checks: single bidder, direct procurement, contract signed before tender closed,
    amount just below a round limit, same supplier winning repeatedly from the same buyer.
    Also returns data problems found in scope, which are not flags.
    """
    return checks.check_red_flags(get_connection(), ocid, buyer)


@mcp.tool()
def file_flag(case_id: str, ocid: str, flag_type: str, finding: str, evidence: str, approved_by: str) -> dict:
    """WRITE. Add one flag to the case file. Needs a named human approver.

    finding: the pattern in plain words. evidence: the facts and numbers behind it.
    Refused if the ocid does not exist, the approver is missing, or the wording decides
    the award or declares wrongdoing.
    """
    try:
        return cases.file_flag(case_id, ocid, flag_type, finding, evidence, approved_by, _known_ocids())
    except cases.CaseError as e:
        return {"status": "refused", "reason": str(e)}


@mcp.tool()
def draft_report(case_id: str, title: str, summary: str, approved_by: str) -> dict:
    """WRITE. Build the committee report (report.md) from the approved flags in a case.

    Needs a named human approver. Each finding in the report cites its OCDS record.
    """
    try:
        return cases.draft_report(case_id, title, summary, approved_by)
    except cases.CaseError as e:
        return {"status": "refused", "reason": str(e)}


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
