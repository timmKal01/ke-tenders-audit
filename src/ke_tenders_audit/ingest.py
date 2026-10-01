"""Turn raw PPRA OCDS downloads into a cleaned file that is safe to publish.

Raw files go in data/raw/ (not committed). The cleaned output,
data/clean/releases.jsonl, is committed so the project runs with no download.

What cleaning does:
- removes personal contact details (contactPoint, street address, postal code)
- drops the tender startDate, which in this feed is the export time, not a real date
- keeps one copy of each release, preferring the most recent
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
CLEAN_FILE = ROOT / "data" / "clean" / "releases.jsonl"

PERSONAL_ADDRESS_FIELDS = ("streetAddress", "postalCode")

# Some publishers type an email or phone number into the company name field.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+?254|\b0)[17]\d{8}\b")


def scrub(text):
    if not isinstance(text, str):
        return text
    return re.sub(r"\s+", " ", PHONE.sub("", EMAIL.sub("", text))).strip()


def clean_party(party: dict) -> dict:
    party = dict(party)
    party.pop("contactPoint", None)
    party["name"] = scrub(party.get("name"))
    if "identifier" in party:
        party["identifier"] = {k: scrub(v) for k, v in party["identifier"].items()}
    if "address" in party:
        party["address"] = {
            k: v for k, v in party["address"].items() if k not in PERSONAL_ADDRESS_FIELDS
        }
    return party


def clean_release(release: dict, source: str) -> dict:
    release = json.loads(json.dumps(release))  # deep copy
    release["parties"] = [clean_party(p) for p in release.get("parties", [])]
    for award in release.get("awards", []):
        for supplier in award.get("suppliers", []):
            supplier["name"] = scrub(supplier.get("name"))
    period = release.get("tender", {}).get("tenderPeriod", {})
    period.pop("startDate", None)
    release["_source"] = source
    return release


def load_raw_files(raw_dir: Path = RAW_DIR) -> list[dict]:
    by_ocid: dict[str, dict] = {}
    for path in sorted(raw_dir.glob("*.json")):
        package = json.loads(path.read_text(encoding="utf-8"))
        for release in package.get("releases", []):
            cleaned = clean_release(release, path.name)
            current = by_ocid.get(cleaned["ocid"])
            if current is None or cleaned.get("date", "") >= current.get("date", ""):
                by_ocid[cleaned["ocid"]] = cleaned
    return list(by_ocid.values())


def main() -> None:
    releases = load_raw_files()
    if not releases:
        sys.exit(f"No raw OCDS files found in {RAW_DIR}")
    CLEAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CLEAN_FILE.open("w", encoding="utf-8") as f:
        for release in sorted(releases, key=lambda r: r["ocid"]):
            f.write(json.dumps(release, ensure_ascii=False) + "\n")
    print(f"Wrote {len(releases)} cleaned releases to {CLEAN_FILE}")


if __name__ == "__main__":
    main()
