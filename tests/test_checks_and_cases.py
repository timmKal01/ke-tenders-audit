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
    flag = cases.file_flag(case_id, KILIFI_OCID, "t", "A pattern — worth a look", "KES 1", "Jane Wanjiku",
                           {KILIFI_OCID})
    assert "—" not in flag["finding"]
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
