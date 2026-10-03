"""The audit checks. Plain functions over DuckDB so they are easy to test.

Every result carries the ocid it came from. A finding without an ocid is a bug.
"""

import statistics
from datetime import datetime

from rapidfuzz import fuzz, process

from .data import supplier_key

# Awards just under these amounts (KES) are worth a second look. These are
# round numbers, not confirmed legal thresholds: check them against the
# PPADA Regulations 2020 before relying on them.
ROUND_AMOUNTS = [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000, 100_000_000]
JUST_BELOW = 0.03          # within 3% below a round amount
REPEAT_WIN_COUNT = 3       # same supplier, same buyer, this many awards or more
MIN_COMPARABLES = 5        # fewer than this and a price comparison is not meaningful


def _rows(con, sql: str, params=()) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, (_plain(v) for v in row))) for row in cur.fetchall()]


def _plain(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, float):
        return round(value, 2)
    return value


def find_buyers(con, text: str, limit: int = 5) -> list[str]:
    names = [r[0] for r in con.execute("SELECT DISTINCT buyer_name FROM tenders WHERE buyer_name IS NOT NULL").fetchall()]
    matches = process.extract(text, names, scorer=fuzz.token_set_ratio, processor=str.lower, limit=limit)
    return [name for name, score, _ in matches if score >= 60]


def buyer_hint(con, buyer: str) -> dict:
    """Explain why a buyer search found no awards, so the agent can recover."""
    tenders = con.execute("SELECT count(*) FROM tenders WHERE buyer_name ILIKE ?", [f"%{buyer}%"]).fetchone()[0]
    if tenders:
        return {"buyer_found": True, "tenders_published": tenders, "awards_published": 0,
                "explanation": "This buyer published tenders but no awards in this data, so there is nothing to audit yet."}
    return {"buyer_found": False, "closest_buyer_names": find_buyers(con, buyer)}


def search_awards(con, buyer=None, item=None, supplier=None, category=None, method=None,
                  date_from=None, date_to=None, limit=10) -> dict:
    where, params = ["1=1"], []
    if buyer:
        where.append("t.buyer_name ILIKE ?"); params.append(f"%{buyer}%")
    if item:
        where.append("(t.title ILIKE ? OR t.item_group ILIKE ?)"); params += [f"%{item}%", f"%{item}%"]
    if supplier:
        where.append("a.supplier_key LIKE ?"); params.append(f"%{supplier_key(supplier)}%")
    if category:
        where.append("t.category = ?"); params.append(category.lower())
    if method:
        where.append("t.method = ?"); params.append(method.lower())
    if date_from:
        where.append("a.date_signed >= ?::TIMESTAMPTZ"); params.append(date_from)
    if date_to:
        where.append("a.date_signed <= ?::TIMESTAMPTZ"); params.append(date_to)

    sql = f"""
        SELECT a.ocid, t.buyer_name AS buyer, left(t.title, 70) AS title, t.item_group, t.method,
               a.amount, a.supplier_name AS supplier, a.date_signed,
               (SELECT count(*) FROM bidders b WHERE b.ocid = a.ocid) AS bidders
        FROM awards a JOIN tenders t USING (ocid)
        WHERE {' AND '.join(where)}
        ORDER BY a.amount DESC NULLS LAST"""
    rows = _rows(con, sql, params)
    zero = [r for r in rows if not r["amount"]]
    result = {
        "matched_awards": len(rows),
        "returned": min(len(rows), limit),
        "awards": rows[:limit],
        "data_notes": [],
    }
    if zero:
        result["data_notes"].append(
            f"{len(zero)} matched awards have amount 0 or missing. This is a data gap, not a free contract. "
            "They are excluded from price comparisons."
        )
    if not rows and buyer:
        result["hint"] = buyer_hint(con, buyer)
    if not rows and not buyer:
        result["hint"] = "No awards matched. Try fewer filters or a broader item word."
    return result


def price_benchmark(con, ocid: str) -> dict:
    awards = _rows(con, """SELECT a.award_id, a.amount, a.supplier_name, t.item_group, t.category, t.title
                           FROM awards a JOIN tenders t USING (ocid) WHERE a.ocid = ?""", [ocid])
    if not awards:
        return {"ocid": ocid, "error": "No award found for this ocid. Check the ocid with search_awards."}

    results = []
    for award in awards:
        entry = {"award_id": award["award_id"], "amount": award["amount"], "supplier": award["supplier_name"],
                 "item_group": award["item_group"]}
        if not award["amount"]:
            entry["status"] = "skipped: amount is 0 or missing (data gap)"
            results.append(entry); continue
        if award["item_group"] == "other":
            entry["status"] = "skipped: title could not be matched to a comparable item group"
            results.append(entry); continue
        comps = _rows(con, """SELECT a.ocid, a.amount FROM awards a JOIN tenders t USING (ocid)
                              WHERE t.item_group = ? AND t.category IS NOT DISTINCT FROM ?
                                AND a.ocid <> ? AND a.amount > 0""",
                      [award["item_group"], award["category"], ocid])
        amounts = [c["amount"] for c in comps]
        entry["comparables"] = len(amounts)
        if len(amounts) < MIN_COMPARABLES:
            entry["status"] = f"insufficient comparables ({len(amounts)} < {MIN_COMPARABLES}); no conclusion"
            results.append(entry); continue
        q1, median, q3 = statistics.quantiles(amounts, n=4)
        upper_fence = q3 + 1.5 * (q3 - q1)
        entry.update(
            median=round(median, 2), q1=round(q1, 2), q3=round(q3, 2),
            ratio_to_median=round(award["amount"] / median, 2),
            outlier=award["amount"] > upper_fence,
            comparable_ocids=[c["ocid"] for c in comps][:5],
            status="compared",
        )
        results.append(entry)
    return {
        "ocid": ocid,
        "title": awards[0]["title"],
        "results": results,
        "caveat": "Awards are lump sums, not unit prices. A high ratio means 'check the scope', not 'overpriced'.",
    }


COMMON_COMPANY_WORDS = {"LTD", "CO", "ENTERPRISES", "INVESTMENTS", "&", "GENERAL", "SUPPLIES", "KENYA"}


def core_name(key: str) -> str:
    """'SILOW SMARTE INVESTMENTS LTD' -> 'SILOW SMARTE', so shared words like LTD don't count as a match."""
    words = [w for w in key.split() if w not in COMMON_COMPANY_WORDS]
    return " ".join(words) or key


def supplier_profile(con, name: str) -> dict:
    keys = [r[0] for r in con.execute("SELECT DISTINCT supplier_key FROM bidders WHERE supplier_key <> ''").fetchall()]
    cores = {k: core_name(k) for k in keys}
    matches = process.extract(core_name(supplier_key(name)), cores, scorer=fuzz.token_sort_ratio, limit=5)
    matches = [(key, score) for _, score, key in matches]
    if not matches or matches[0][1] < 90:
        return {"query": name, "error": "No confident supplier match.",
                "candidates": [k for k, s in matches if s >= 60]}
    key = matches[0][0]
    bids = _rows(con, "SELECT ocid, is_winner FROM bidders WHERE supplier_key = ?", [key])
    wins = _rows(con, """SELECT a.ocid, a.amount, t.buyer_name AS buyer, left(t.title, 60) AS title,
                                (SELECT count(*) FROM bidders b WHERE b.ocid = a.ocid) AS bidders
                         FROM awards a JOIN tenders t USING (ocid) WHERE a.supplier_key = ?""", [key])
    by_buyer: dict[str, int] = {}
    for w in wins:
        by_buyer[w["buyer"]] = by_buyer.get(w["buyer"], 0) + 1
    return {
        "supplier": key,
        "match_score": round(matches[0][1]),
        "other_close_names": [k for k, s in matches[1:] if s >= 90],
        "bids_listed": len(bids),
        "awards_won": len(wins),
        "win_rate": round(len({w["ocid"] for w in wins}) / len(bids), 2) if bids else None,
        "total_awarded_kes": round(sum(w["amount"] or 0 for w in wins), 2),
        "awards_by_buyer": by_buyer,
        "single_bidder_wins": [w["ocid"] for w in wins if w["bidders"] == 1],
        "awards": wins[:8],
        "note": "Company records only. Bids are only visible on awarded tenders in this feed.",
    }


def record_facts(con, ocid: str) -> dict:
    """The facts a flag must agree with: winners, amounts, bidders, method, buyer."""
    awards = _rows(con, "SELECT supplier_name, supplier_key, amount FROM awards WHERE ocid = ?", [ocid])
    tender = _rows(con, "SELECT buyer_name, method, left(title, 80) AS title FROM tenders WHERE ocid = ?", [ocid])
    bidders = _rows(con, "SELECT name, supplier_key FROM bidders WHERE ocid = ?", [ocid])
    t = tender[0] if tender else {}
    return {
        "buyer": t.get("buyer_name"), "title": t.get("title"), "method": t.get("method"),
        "winners": [a["supplier_name"] for a in awards], "amounts_kes": [a["amount"] for a in awards],
        "bidders": len(bidders),
        "_keys": {a["supplier_key"] for a in awards if a["supplier_key"]} | {b["supplier_key"] for b in bidders},
    }


PRICE_FLAG_WORDS = ("price", "outlier", "overpric", "value")


def price_flag_problem(con, ocid: str, flag_type: str) -> str | None:
    """A price flag is only allowed when price_benchmark marks the award as an outlier.

    Eval T06: the model filed a price outlier on an award at 3.05 times the median that the
    benchmark had marked as within the normal range.
    """
    if not any(w in (flag_type or "").lower() for w in PRICE_FLAG_WORDS):
        return None
    results = price_benchmark(con, ocid).get("results", [])
    if any(r.get("outlier") for r in results):
        return None
    detail = "; ".join(f"ratio {r['ratio_to_median']}" if "ratio_to_median" in r else r.get("status", "")
                       for r in results) or "no award found"
    return (f"price_benchmark did not mark this award as an outlier ({detail}). Only flag prices the "
            "benchmark marks as outliers. Mention other high ratios in the summary instead.")


def wrong_suppliers(con, text: str, record_keys: set[str]) -> list[str]:
    """Companies named in the text that are not bidders or winners on the record.

    Compares distinctive names, so 'PETLICO AGENCIES' and 'PETLICO AGENCIES LTD' count as one company.
    """
    on_record = {core_name(k) for k in record_keys}
    return sorted({core_name(k) for k in suppliers_mentioned(con, text)} - on_record)


def suppliers_mentioned(con, text: str) -> set[str]:
    """Known supplier companies whose distinctive name appears in the text."""
    normalised = f" {supplier_key(text)} "
    keys = {r[0] for r in con.execute("SELECT DISTINCT supplier_key FROM bidders WHERE supplier_key <> ''").fetchall()}
    return {k for k in keys if len(core_name(k)) >= 5 and f" {core_name(k)} " in normalised}


def check_red_flags(con, ocid=None, buyer=None) -> dict:
    if not ocid and not buyer:
        return {"error": "Give an ocid or a buyer name to limit the scope."}
    scope_sql, params = ("t.ocid = ?", [ocid]) if ocid else ("t.buyer_name ILIKE ?", [f"%{buyer}%"])
    awards = _rows(con, f"""
        SELECT a.ocid, a.award_id, a.amount, a.supplier_name, a.supplier_key, a.date_signed,
               t.buyer_name, t.method, t.tender_end, t.title,
               (SELECT count(*) FROM bidders b WHERE b.ocid = a.ocid) AS bidders
        FROM awards a JOIN tenders t USING (ocid) WHERE {scope_sql}""", params)
    if not awards:
        hint = buyer_hint(con, buyer) if buyer else {}
        return {"scope": ocid or buyer, "awards_checked": 0, "flags": [], "hint": hint or "No awards in scope."}

    flags = []
    seen_single = set()
    for a in awards:
        if a["bidders"] == 1 and a["ocid"] not in seen_single:
            seen_single.add(a["ocid"])
            flags.append(_flag("single_bidder", a, f"Only one bidder is listed ({a['method']} method).",
                               "Low competition can mean poor value or a restrictive specification."))
        if a["method"] == "direct":
            flags.append(_flag("direct_procurement", a, "Procurement method is 'direct'.",
                               "Direct procurement needs a documented justification."))
        if a["date_signed"] and a["tender_end"] and a["date_signed"] < a["tender_end"]:
            flags.append(_flag("signed_before_close", a,
                               f"Contract signed {a['date_signed']}, tender closed {a['tender_end']}.",
                               "Signing before bids close is irregular, or the dates were entered wrongly."))
        for limit in ROUND_AMOUNTS:
            if a["amount"] and limit * (1 - JUST_BELOW) <= a["amount"] < limit:
                flags.append(_flag("just_below_round_amount", a,
                                   f"Amount KES {a['amount']:,.2f} is within 3% below KES {limit:,}.",
                                   "Values just under a limit can indicate splitting or threshold avoidance. "
                                   "Confirm the actual PPADA threshold before concluding anything."))

    repeat = _rows(con, f"""
        SELECT a.supplier_key, t.buyer_name, count(DISTINCT a.ocid) AS n, list(DISTINCT a.ocid) AS ocids
        FROM awards a JOIN tenders t USING (ocid)
        WHERE {scope_sql} AND a.supplier_key IS NOT NULL
        GROUP BY 1, 2 HAVING count(DISTINCT a.ocid) >= ?""", params + [REPEAT_WIN_COUNT])
    for r in repeat:
        flags.append({
            "flag_type": "repeat_winner_same_buyer",
            "ocid": r["ocids"][0],
            "related_ocids": r["ocids"],
            "evidence": f"{r['supplier_key']} won {r['n']} awards from {r['buyer_name']}.",
            "why_it_matters": "Repeated wins can be legitimate, but concentration deserves a look at competition.",
        })

    issues = _rows(con, f"""SELECT d.ocid, d.issue, d.detail FROM data_issues d JOIN tenders t USING (ocid)
                            WHERE {scope_sql}""", params)
    counts: dict[str, int] = {}
    for f in flags:
        counts[f["flag_type"]] = counts.get(f["flag_type"], 0) + 1
    return {
        "scope": ocid or buyer,
        "awards_checked": len(awards),
        "flag_counts": counts,  # first, so a shortened copy of this result still carries the totals
        "flags": _interleave(flags)[:15],
        "flags_total": len(flags),
        "data_problems_total": len(issues),
        "data_problems": issues[:5],
        "note": "Flags are patterns for a human to review, not findings of wrongdoing.",
    }


def _interleave(flags: list[dict]) -> list[dict]:
    """Alternate flag types, so a capped or shortened list still shows every kind."""
    by_type: dict[str, list[dict]] = {}
    for f in flags:
        by_type.setdefault(f["flag_type"], []).append(f)
    out = []
    while any(by_type.values()):
        for group in by_type.values():
            if group:
                out.append(group.pop(0))
    return out


def check_totals(con, buyers: list[str]) -> list[dict]:
    """Pattern counts per buyer, computed from the data for the report (not by the model)."""
    rows = []
    for buyer in sorted(set(b for b in buyers if b)):
        result = check_red_flags(con, buyer=buyer)
        rows.append({"buyer": buyer, "awards_checked": result["awards_checked"],
                     "flag_counts": result.get("flag_counts", {}),
                     "data_problems": result.get("data_problems_total", 0)})
    return rows


def _flag(flag_type, award, evidence, why):
    return {"flag_type": flag_type, "ocid": award["ocid"], "supplier": award["supplier_name"],
            "evidence": evidence, "why_it_matters": why}
