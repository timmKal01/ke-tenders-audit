"""The case file: the only thing the agent is allowed to write.

Rules enforced here, not just in the prompt:
- every flag must point to an ocid that exists in the data
- every write must name the human who approved it
- flags describe patterns; wording that decides an award or declares guilt is refused
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "cases"

CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
NOT_A_PERSON = {"", "agent", "ai", "assistant", "system", "model", "auto", "none"}
DECISION_WORDS = re.compile(
    r"\b(award (the tender|the contract|it) to|should be awarded|recommend(ed)? (awarding|award to)|"
    r"disqualif(y|ied)|is guilty|fraudulent|corrupt(ion)?|blacklist)\b",
    re.IGNORECASE,
)


class CaseError(ValueError):
    pass


def plain_text(text: str) -> str:
    """House style: no em or en dashes in anything we write."""
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text or "")
    return text.strip()


def _check_approver(approved_by: str) -> str:
    name = (approved_by or "").strip()
    if name.lower() in NOT_A_PERSON or len(name) < 3:
        raise CaseError("approved_by must be the full name of the human reviewer who approved this write.")
    return name


def _case_dir(case_id: str) -> Path:
    if not CASE_ID.match(case_id or ""):
        raise CaseError("case_id must be lowercase letters, digits and hyphens, e.g. 'kakrao-tvc-fy2026'.")
    return CASES_DIR / case_id


def load_flags(case_id: str) -> list[dict]:
    path = _case_dir(case_id) / "flags.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def file_flag(case_id: str, ocid: str, flag_type: str, finding: str, evidence: str,
              approved_by: str, known_ocids: set[str], record: dict | None = None) -> dict:
    approver = _check_approver(approved_by)
    if ocid not in known_ocids:
        raise CaseError(f"ocid '{ocid}' is not in the data. Every flag must cite a real record.")
    for field in (finding, evidence):
        if DECISION_WORDS.search(field or ""):
            raise CaseError("Describe the pattern and its source. Do not decide the award or declare wrongdoing.")
    if not (finding or "").strip() or not (evidence or "").strip():
        raise CaseError("finding and evidence are both required.")

    flags = load_flags(case_id)
    if any(f["ocid"] == ocid and f["flag_type"] == flag_type for f in flags):
        return {"status": "already_filed", "case_id": case_id, "ocid": ocid, "flag_type": flag_type}

    flag = {
        "flag_id": f"F{len(flags) + 1:03d}",
        "ocid": ocid,
        "flag_type": flag_type,
        "finding": plain_text(finding),
        "evidence": plain_text(evidence),
        "record": record or {},
        "approved_by": approver,
        "filed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    flags.append(flag)
    folder = _case_dir(case_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "flags.json").write_text(json.dumps(flags, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "filed", "case_id": case_id, **flag}


def _record_line(r: dict) -> str:
    amounts = ", ".join(f"KES {a:,.2f}" if a else "KES 0 (data gap)" for a in r.get("amounts_kes", []))
    return (f"buyer {r.get('buyer')}; method {r.get('method')}; winner(s) {', '.join(r.get('winners') or ['none'])}; "
            f"amount(s) {amounts or 'none'}; bidders listed {r.get('bidders')}")


def complete_sentences(summary: str) -> tuple[str, bool]:
    """Return the summary ending on a full sentence, and whether it had to be trimmed.

    The model provider sometimes cuts long tool arguments at about 500 characters. Refusing
    such a summary made the model retry the same length in a loop, so it is trimmed instead.
    """
    text = (summary or "").rstrip()
    if text.endswith((".", "!", "?", ")")):
        return text, False
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", text)]
    if ends and ends[-1] >= 30:
        return text[:ends[-1]], True
    raise CaseError("The summary has no complete sentence. Write 2 to 4 short sentences, under 400 characters.")


def draft_report(case_id: str, title: str, summary: str, approved_by: str,
                 totals: list[dict] | None = None) -> dict:
    approver = _check_approver(approved_by)
    flags = load_flags(case_id)
    if not flags:
        raise CaseError("No approved flags in this case yet. File flags before drafting the report.")
    if DECISION_WORDS.search(summary or ""):
        raise CaseError("The summary must not decide the award or declare wrongdoing.")
    summary, trimmed = complete_sentences(summary)

    lines = [
        f"# {plain_text(title)}",
        "",
        f"Case: `{case_id}`  ",
        f"Prepared: {datetime.now(timezone.utc).date().isoformat()} by Ke-Tenders Audit  ",
        f"Approved for the committee by: {approver}",
        "",
        "> This file supports the evaluation committee. It does not award, disqualify or accuse anyone.",
        "> Each finding is a pattern worth checking, with the OCDS record it came from.",
        "",
        "## Summary",
        "",
        plain_text(summary),
        "",
        "## Findings",
        "",
    ]
    for f in flags:
        lines += [
            f"### {f['flag_id']}. {f['flag_type'].replace('_', ' ').capitalize()}",
            "",
            f"- **Finding:** {f['finding']}",
            f"- **Evidence:** {f['evidence']}",
            f"- **Source:** OCDS record `{f['ocid']}` (PPRA, tenders.go.ke)",
            *([f"- **Record says:** {_record_line(f['record'])}"] if f.get("record") else []),
            f"- **Approved by:** {f['approved_by']} on {f['filed_at']}",
            "",
        ]
    if totals:
        filed: dict[str, int] = {}
        for f in flags:
            filed[f["flag_type"]] = filed.get(f["flag_type"], 0) + 1
        lines += [
            "## What the integrity checks found",
            "",
            "Computed directly from the data, not written by the model. If the summary above disagrees "
            "with this table, the table is correct.",
            "",
            "| Buyer | Awards checked | Pattern | Found | Filed above |",
            "|---|---|---|---|---|",
        ]
        for t in totals:
            counts = t["flag_counts"] or {"none": 0}
            for pattern, n in sorted(counts.items()):
                lines.append(f"| {t['buyer']} | {t['awards_checked']} | {pattern.replace('_', ' ')} | {n} | "
                             f"{filed.get(pattern, 0)} |")
            for pattern, n in sorted(filed.items()):
                if pattern not in counts:  # e.g. price outliers come from price_benchmark, not the integrity check
                    lines.append(f"| {t['buyer']} | | {pattern.replace('_', ' ')} (other checks) | n/a | {n} |")
            lines.append(f"| {t['buyer']} | | data problems (not flags) | {t['data_problems']} | |")
        lines += ["", "Patterns found but not filed were judged less important by the agent. "
                      "The committee can ask for any of them.", ""]
    lines += [
        "## Limits of this review",
        "",
        "- The PPRA feed publishes tenders and awards, not the bid documents, so bids themselves were not evaluated.",
        "- Award values are lump sums, so price comparisons point to scope questions, not proven overpricing.",
        "- Records with missing or zero values were excluded and are listed in the audit log.",
    ]
    path = _case_dir(case_id) / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {"status": "drafted", "case_id": case_id, "path": str(path.relative_to(ROOT)), "findings": len(flags)}
    if trimmed:
        result["note"] = "The summary was cut off mid-sentence, so it was trimmed to its last complete sentence."
    return result
