"""Load the cleaned OCDS releases into DuckDB tables the tools can query.

Tables:
  tenders      one row per tender (ocid)
  awards       one row per award and supplier
  bidders      one row per bidder listed on an awarded tender
  data_issues  problems found in the source data, with the ocid they came from
"""

import json
import re
from functools import lru_cache
from pathlib import Path

import duckdb
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[2]
CLEAN_FILE = ROOT / "data" / "clean" / "releases.jsonl"

# Free-text titles grouped into comparable items. First match wins.
ITEM_GROUPS = [
    ("training practical items", r"CDACC|KNEC|PRACTICAL ITEMS|TRAINING MATERIALS|TRAINING EQUIPMENT"),
    ("events and audio-visual", r"EVENT|AUDIO|VIDEOGRAPHY|\bTENTS?\b|D.COR|P\.A SYSTEM"),
    ("branded merchandise", r"BRANDED|T-? ?SHIRT|JACKET|RIBBON"),
    ("water supplies and chemicals", r"WATER METER|WATER PIPE|WATER FITTING|WATER TREATMENT|CITRIC ACID|CHEMICAL"),
    ("security services", r"\bSECURITY\b"),
    ("insurance", r"\bINSURANCE\b"),
    ("fumigation and pest control", r"FUMIGATION|PEST CONTROL"),
    ("cleaning and sanitary", r"CLEANING|SANITARY|HOUSEKEEPING|DETERGENT"),
    ("stationery and printing", r"STATIONER|PRINTING|PRINTED|OFFICE SUPPLIES|REGISTERS|MINUTE BOOK|TONN?ERS?\b"),
    ("computers and ICT", r"COMPUTER|LAPTOP|\bICT\b|PRINTER|SOFTWARE|NETWORK"),
    ("hardware materials", r"HARDWARE"),
    ("motor vehicle maintenance", r"MOTOR VEHICLE|MAINTAINANCE OF VEHICLE|VEHICLE REPAIR|TYRES"),
    ("fuel and lubricants", r"\bFUEL\b|LUBRICANT|DIESEL|PETROL"),
    ("firewood", r"FIREWOOD"),
    ("milk and dairy", r"\bMILK\b|DAIRY"),
    ("meat", r"\bMEAT\b|\bBEEF\b|\bGOAT\b|CHICKEN"),
    ("fruits and vegetables", r"FRUIT|VEGETABLE"),
    ("cereals and dry food", r"MAIZE|RICE|BEANS|CEREAL|DRY GOODS|FLOUR|SUGAR|FOODSTUFF"),
    ("catering and conference", r"CATERING|CONFERENCE|ACCOMMODATION|ACCOMODATION|VENUE|HOTEL"),
    ("electrical items", r"ELECTRICAL|EMERGENCY LIGHT|BULBS|CABLES"),
    ("kitchen equipment", r"KITCHEN|FREEZER|COOKER"),
    ("uniforms and textiles", r"UNIFORM|CURTAIN|TEXTILE|BEDDING"),
    ("medical supplies", r"MEDICAL|HEALTH PRODUCTS|PHARMACEUTICAL|DRUGS|LABORATORY|REAGENT|ANTI ?SERUM|VACCINE"),
    ("travel", r"AIR ?TICKET|TRAVEL"),
    ("construction and civil works", r"CONSTRUCTION|REHABILITATION|RENOVATION|BUI?LDING|PIPELINE|ROAD|DRAINAGE|CLASSROOM|REPAIR WORKS|CABRO|WATER PROJECT|DESILTING"),
]

SUPPLIER_WORDS = [
    (r"\bLIMITED\b", "LTD"),
    (r"\bCOMPANY\b", "CO"),
    (r"\bENTERPRISE\b", "ENTERPRISES"),
    (r"\bAND\b", "&"),
]


def item_group(title: str) -> str:
    upper = (title or "").upper()
    for name, pattern in ITEM_GROUPS:
        if re.search(pattern, upper):
            return name
    return "other"


def supplier_key(name: str) -> str:
    """Normalise a company name so 'X Limited' and 'X LTD.' match."""
    key = (name or "").upper()
    key = re.sub(r"[^\w&]+", " ", key)
    for pattern, repl in SUPPLIER_WORDS:
        key = re.sub(pattern, repl, key)
    key = re.sub(r"^THE\s+", "", key)
    return re.sub(r"\s+", " ", key).strip()


def _rows_from_release(r: dict):
    tender = r.get("tender", {})
    buyer = r.get("buyer") or {}
    ocid = r["ocid"]
    title = (tender.get("title") or "").strip()
    tender_end = tender.get("tenderPeriod", {}).get("endDate")
    tender_row = dict(
        ocid=ocid,
        release_date=r.get("date"),
        buyer_id=buyer.get("id"),
        buyer_name=(buyer.get("name") or "").strip() or None,
        title=title,
        item_group=item_group(title),
        method=tender.get("procurementMethod"),
        category=tender.get("mainProcurementCategory"),
        tender_end=tender_end,
        source_file=r.get("_source"),
    )
    issues = []
    if not tender_row["buyer_name"]:
        issues.append((ocid, "missing_buyer_name", "buyer has no name"))
    if not tender_row["category"]:
        issues.append((ocid, "missing_category", "mainProcurementCategory is empty"))

    contracts = {c.get("awardID"): c for c in r.get("contracts", [])}
    award_rows = []
    winner_ids = set()
    for a in r.get("awards", []):
        value = a.get("value") or {}
        amount = value.get("amount")
        contract = contracts.get(a.get("id"), {})
        signed = contract.get("dateSigned")
        suppliers = a.get("suppliers") or [{}]
        if amount in (None, 0):
            issues.append((ocid, "zero_or_missing_amount", f"award {a.get('id')} has amount {amount}"))
        if not a.get("suppliers"):
            issues.append((ocid, "award_without_supplier", f"award {a.get('id')} lists no supplier"))
        for s in suppliers:
            winner_ids.add(s.get("id"))
            award_rows.append(dict(
                award_id=a.get("id"),
                ocid=ocid,
                amount=amount,
                currency=value.get("currency"),
                contract_start=(a.get("contractPeriod") or {}).get("startDate"),
                contract_end=(a.get("contractPeriod") or {}).get("endDate"),
                date_signed=signed,
                supplier_id=s.get("id"),
                supplier_name=(s.get("name") or "").strip() or None,
                supplier_key=supplier_key(s.get("name") or "") or None,
            ))

    bidder_rows = []
    if r.get("awards"):
        for p in r.get("parties", []):
            if "supplier" in p.get("roles", []) or "tenderer" in p.get("roles", []):
                name = (p.get("name") or (p.get("identifier") or {}).get("legalName") or "").strip()
                bidder_rows.append(dict(
                    ocid=ocid,
                    party_id=p.get("id"),
                    name=name,
                    supplier_key=supplier_key(name),
                    is_winner=p.get("id") in winner_ids,
                ))
    return tender_row, award_rows, bidder_rows, issues


def build_connection(clean_file: Path = CLEAN_FILE) -> duckdb.DuckDBPyConnection:
    tenders, awards, bidders, issues = [], [], [], []
    with clean_file.open(encoding="utf-8") as f:
        for line in f:
            t, a, b, i = _rows_from_release(json.loads(line))
            tenders.append(t)
            awards.extend(a)
            bidders.extend(b)
            issues.extend(i)

    con = duckdb.connect()
    con.execute("""CREATE TABLE tenders (ocid VARCHAR PRIMARY KEY, release_date TIMESTAMPTZ,
        buyer_id VARCHAR, buyer_name VARCHAR, title VARCHAR, item_group VARCHAR, method VARCHAR,
        category VARCHAR, tender_end TIMESTAMPTZ, source_file VARCHAR)""")
    con.execute("""CREATE TABLE awards (award_id VARCHAR, ocid VARCHAR, amount DOUBLE, currency VARCHAR,
        contract_start TIMESTAMPTZ, contract_end TIMESTAMPTZ, date_signed TIMESTAMPTZ,
        supplier_id VARCHAR, supplier_name VARCHAR, supplier_key VARCHAR)""")
    con.execute("""CREATE TABLE bidders (ocid VARCHAR, party_id VARCHAR, name VARCHAR,
        supplier_key VARCHAR, is_winner BOOLEAN)""")
    con.execute("CREATE TABLE data_issues (ocid VARCHAR, issue VARCHAR, detail VARCHAR)")

    _insert(con, "tenders", tenders)
    _insert(con, "awards", awards)
    _insert(con, "bidders", bidders)
    _insert(con, "data_issues", [dict(ocid=o, issue=i, detail=d) for o, i, d in issues])

    # Date problems are easier to find in SQL once the tables exist.
    con.execute("""INSERT INTO data_issues
        SELECT DISTINCT a.ocid, 'contract_ends_before_start',
               'award ' || a.award_id || ' ends before it starts'
        FROM awards a WHERE a.contract_end < a.contract_start""")
    return con


def _insert(con, table: str, rows: list[dict]) -> None:
    """Bulk insert through an Arrow table (row-by-row inserts take minutes)."""
    if not rows:
        return
    arrow_rows = pa.Table.from_pylist(rows)
    con.register("_rows", arrow_rows)
    con.execute(f"INSERT INTO {table} ({', '.join(arrow_rows.column_names)}) SELECT * FROM _rows")
    con.unregister("_rows")


@lru_cache(maxsize=1)
def get_connection() -> duckdb.DuckDBPyConnection:
    return build_connection()


@lru_cache(maxsize=1)
def releases_by_ocid() -> dict[str, dict]:
    with CLEAN_FILE.open(encoding="utf-8") as f:
        return {r["ocid"]: r for r in map(json.loads, f)}
