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
                  date_from=None, date_to=None, limit=25) -> dict:
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
        SELECT a.ocid, t.buyer_name AS buyer, t.title, t.item_group, t.method, t.category,
               a.award_id, a.amount, a.supplier_name AS supplier, a.date_signed,
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
            comparable_ocids=[c["ocid"] for c in comps][:10],
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
    wins = _rows(con, """SELECT a.ocid, a.amount, t.buyer_name AS buyer, t.title,
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
        "awards": wins[:20],
        "note": "Company records only. Bids are only visible on awarded tenders in this feed.",
    }


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
    return {
        "scope": ocid or buyer,
        "awards_checked": len(awards),
        "flags": flags,
        "data_problems": issues,
        "note": "Flags are patterns for a human to review, not findings of wrongdoing.",
    }


def _flag(flag_type, award, evidence, why):
    return {"flag_type": flag_type, "ocid": award["ocid"], "award_id": award["award_id"],
            "supplier": award["supplier_name"], "buyer": award["buyer_name"],
            "evidence": evidence, "why_it_matters": why}
