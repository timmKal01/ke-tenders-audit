import json
import uuid

import pytest

from ke_tenders_audit import cases, checks
from ke_tenders_audit.data import get_connection, item_group, supplier_key
from ke_tenders_audit.ingest import CLEAN_FILE, scrub

KILIFI_OCID = "ocds-5whusi-290427-KCG/WSNRM/2122735/2025/2026."


@pytest.fixture(scope="module")
def con():
    return get_connection()


def test_clean_file_has_no_personal_contacts():
    text = CLEAN_FILE.read_text(encoding="utf-8")
    assert "contactPoint" not in text
    assert "@gmail" not in text.lower()


def test_scrub_removes_email_and_phone_from_names():
    assert scrub("Acme Supplies Ltd acme.supplies@example.com\n") == "Acme Supplies Ltd"
    assert scrub("Acme Ltd 0712345678") == "Acme Ltd"


def test_supplier_key_normalises_company_words():
    assert supplier_key("Kioli Investments Limited.") == supplier_key("KIOLI INVESTMENTS LTD")


def test_item_group_examples():
    assert item_group("SUPPLY AND DELIVERY OF STATIONERY") == "stationery and printing"
    assert item_group("CDACC PLUMBING PRACTICAL ITEMS") == "training practical items"


def test_known_awards_count(con):
    assert con.execute("SELECT count(DISTINCT ocid) FROM awards").fetchone()[0] == 136


def test_zero_amounts_are_reported_not_hidden(con):
    issues = con.execute("SELECT count(*) FROM data_issues WHERE issue = 'zero_or_missing_amount'").fetchone()[0]
    assert issues == 31


def test_buyer_with_no_awards_is_explained(con):
    hint = checks.search_awards(con, buyer="Kiambu County")["hint"]
    assert hint["buyer_found"] and hint["awards_published"] == 0


def test_misspelt_buyer_gets_suggestions(con):
    hint = checks.search_awards(con, buyer="Kilifi Countyy")["hint"]
    assert "Kilifi County Government" in hint["closest_buyer_names"]


def test_just_below_round_amount_flag(con):
    flags = checks.check_red_flags(con, ocid=KILIFI_OCID)["flags"]
    assert [f["flag_type"] for f in flags] == ["just_below_round_amount"]


def test_price_benchmark_refuses_without_comparables(con):
    result = checks.price_benchmark(con, KILIFI_OCID)["results"][0]
    assert result["status"].startswith("insufficient comparables")


def test_supplier_match_ignores_common_words(con):
    profile = checks.supplier_profile(con, "Silow Smarte Investments Limited")
    assert profile["awards_won"] == 5 and profile["other_close_names"] == []


def test_every_flag_has_a_real_ocid(con):
    known = {r[0] for r in con.execute("SELECT ocid FROM tenders").fetchall()}
    for buyer in ["PC KINYANJUI", "KAKRAO", "Judiciary"]:
        for flag in checks.check_red_flags(con, buyer=buyer)["flags"]:
            assert flag["ocid"] in known


@pytest.mark.parametrize("approver", ["", "agent", "AI", "  "])
def test_write_needs_a_named_human(con, approver):
    with pytest.raises(cases.CaseError):
        cases.file_flag("test-x", KILIFI_OCID, "t", "finding", "evidence", approver, {KILIFI_OCID})


@pytest.mark.parametrize("finding", ["The contract should be awarded to Kioli.", "This is fraudulent."])
def test_decision_language_is_refused(finding):
    with pytest.raises(cases.CaseError):
        cases.file_flag("test-x", KILIFI_OCID, "t", finding, "evidence", "Jane Wanjiku", {KILIFI_OCID})


def test_unknown_ocid_is_refused():
    with pytest.raises(cases.CaseError):
        cases.file_flag("test-x", "ocds-made-up", "t", "finding", "evidence", "Jane Wanjiku", {KILIFI_OCID})


def test_em_dashes_are_removed():
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    flag = cases.file_flag(case_id, KILIFI_OCID, "t", "A pattern \u2014 worth a look", "KES 1", "Jane Wanjiku",
                           {KILIFI_OCID})
    assert "\u2014" not in flag["finding"]
    assert json.loads((cases.CASES_DIR / case_id / "flags.json").read_text(encoding="utf-8"))[0]["flag_id"] == "F001"


def test_flag_naming_the_wrong_supplier_is_refused():
    """Dev run: the model said MATSON won a tender that SALGAD won."""
    from ke_tenders_audit import server
    out = json.loads(server.file_flag(
        "test-wrong-supplier", "ocds-5whusi-302433-PCKTTI-163", "single_bidder",
        "The hardware award had one bidder.", "MATSON GENERAL ENTERPRISES is the sole listed bidder.",
        "Jane Wanjiku"))
    assert out["status"] == "refused"
    assert "SALGAD INVESTMENT LIMITED" in out["reason"]


def test_flag_naming_the_right_supplier_carries_record_facts():
    from ke_tenders_audit import server
    out = json.loads(server.file_flag(
        f"test-{uuid.uuid4().hex[:8]}", "ocds-5whusi-302433-PCKTTI-163", "single_bidder",
        "The hardware award had one bidder.", "SALGAD INVESTMENT LIMITED is the sole listed bidder.",
        "Jane Wanjiku"))
    assert out["status"] == "filed"
    assert out["record"]["bidders"] == 1 and out["record"]["winners"] == ["SALGAD INVESTMENT LIMITED"]


def test_red_flag_totals_come_first_and_types_alternate(con):
    from ke_tenders_audit import server
    out = server.check_red_flags(buyer="PC KINYANJUI TECHNICAL TRAINING INSTITUTE")
    assert out.index('"flag_counts"') < out.index('"flags"')
    head = out[:400]                                   # what survives shortening in later steps
    assert '"single_bidder":8' in head and '"repeat_winner_same_buyer":4' in head
    types = [f["flag_type"] for f in json.loads(out)["flags"][:3]]
    assert len(set(types)) == 3


def test_report_carries_totals_computed_from_data():
    """Dev run: the model's summary said no repeat-winner or signed-before-close patterns existed."""
    from ke_tenders_audit import server
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    server.file_flag(case_id, "ocds-5whusi-302526-PCKTTI-172", "single_bidder",
                     "One bidder.", "IMOTH INSURANCE was the only bidder.", "Jane Wanjiku")
    out = json.loads(server.draft_report(case_id, "Review", "Three single-bidder awards. Nothing else.",
                                         "Jane Wanjiku"))
    text = (cases.CASES_DIR / case_id / "report.md").read_text(encoding="utf-8")
    assert out["status"] == "drafted"
    assert "| repeat winner same buyer | 4 | 0 |" in text
    assert "| signed before close | 1 | 0 |" in text
    assert "| single bidder | 8 | 1 |" in text


def test_cut_off_summary_is_refused():
    """Dev run: the model's summary ended mid-sentence with 'so neither'."""
    from ke_tenders_audit import server
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    server.file_flag(case_id, "ocds-5whusi-302526-PCKTTI-172", "single_bidder",
                     "One bidder.", "IMOTH INSURANCE was the only bidder.", "Jane Wanjiku")
    out = json.loads(server.draft_report(case_id, "Review", "The insurance award has no comparables, so neither",
                                         "Jane Wanjiku"))
    assert out["status"] == "refused" and "cut off" in out["reason"]


def test_price_outlier_gets_its_own_row():
    from ke_tenders_audit import server
    case_id = f"test-{uuid.uuid4().hex[:8]}"
    server.file_flag(case_id, "ocds-5whusi-302523-PCKTTI-112", "price_outlier", "Far above similar awards.",
                     "CAGE DYNAMICS KES 2,898,800, 7.14 times the median.", "Jane Wanjiku")
    server.draft_report(case_id, "Review", "One price outlier.", "Jane Wanjiku")
    text = (cases.CASES_DIR / case_id / "report.md").read_text(encoding="utf-8")
    assert "| price outlier (other checks) | n/a | 1 |" in text


def test_same_company_spelt_with_and_without_ltd_is_not_a_mismatch():
    """Live demo: 'PETLICO AGENCIES' (the winner) was flagged as a mismatch against 'PETLICO AGENCIES LTD'."""
    from ke_tenders_audit import server
    out = json.loads(server.file_flag(
        f"test-{uuid.uuid4().hex[:8]}", "ocds-5whusi-304735-PCKTTI-140", "price_outlier",
        "Award amount is a statistical outlier versus comparable training practical items awards.",
        "Awarded 177,000 KES to PETLICO AGENCIES; median of 48 comparables is 36,435 KES.", "Jane Wanjiku"))
    assert out["status"] == "filed", out
