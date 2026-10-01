# Procurement Review: PC Kinyanjui Technical Training Institute

Case: `review-c41937`  
Prepared: 2026-10-01 by Ke-Tenders Audit  
Approved for the committee by: Timkev

> This file supports the evaluation committee. It does not award, disqualify or accuse anyone.
> Each finding is a pattern worth checking, with the OCDS record it came from.

## Summary

Reviewed 31 published awards for PC Kinyanjui Technical Training Institute. One award is a strong price outlier (ICT materials, 7.14x the comparable median). Three awards were issued under an open method with only one bidder listed. No direct-method, repeat-winner, or signed-before-close flags were returned by the integrity check. Data gaps (zero amounts) are noted but not treated as flags.

## Findings

### F001. Price outlier

- **Finding:** The ICT materials award is a strong price outlier relative to comparable awards in the same item group.
- **Evidence:** Awarded 2,898,800 KES to CAGE DYNAMICS; median of 5 comparables is 406,121.8 KES (Q1 365,295, Q3 984,900); ratio to median 7.14.
- **Source:** OCDS record `ocds-5whusi-302523-PCKTTI-112` (PPRA, tenders.go.ke)
- **Record says:** buyer PC KINYANJUI TECHNICAL TRAINING INSTITUTE; method open; winner(s) CAGE DYNAMICS; amount(s) KES 2,898,800.00; bidders listed 3
- **Approved by:** Timkev on 2026-10-01T16:54:54+00:00

### F002. Single bidder

- **Finding:** The meat, dry goods and dairy tender was awarded under an open method with only one bidder listed.
- **Evidence:** KERRYJAY ENTERPRISES is the sole listed bidder; recorded amount is 0.0 KES, which is a data gap rather than a price signal.
- **Source:** OCDS record `ocds-5whusi-302418-PCKTTI-146` (PPRA, tenders.go.ke)
- **Record says:** buyer PC KINYANJUI TECHNICAL TRAINING INSTITUTE; method open; winner(s) KERRYJAY ENTERPRISES; amount(s) KES 0 (data gap); bidders listed 1
- **Approved by:** Timkev on 2026-10-01T16:54:54+00:00

### F003. Single bidder

- **Finding:** The second single-bidder award in the buyer's portfolio was issued under an open method.
- **Evidence:** check_red_flags returned a single_bidder flag for this ocid; only one bidder is listed on the record.
- **Source:** OCDS record `ocds-5whusi-302433-PCKTTI-163` (PPRA, tenders.go.ke)
- **Record says:** buyer PC KINYANJUI TECHNICAL TRAINING INSTITUTE; method open; winner(s) SALGAD INVESTMENT LIMITED; amount(s) KES 0 (data gap); bidders listed 1
- **Approved by:** Timkev on 2026-10-01T16:54:55+00:00

### F004. Single bidder

- **Finding:** IMOTH INSURANCE won this award as the only listed bidder, and it is the company's only award in the feed.
- **Evidence:** Supplier profile shows 1 bid listed, 1 award won, 100% win rate, 105,000 KES total, sole buyer PC Kinyanjui Technical Training Institute.
- **Source:** OCDS record `ocds-5whusi-302526-PCKTTI-172` (PPRA, tenders.go.ke)
- **Record says:** buyer PC KINYANJUI TECHNICAL TRAINING INSTITUTE; method open; winner(s) IMOTH INSURANCE; amount(s) KES 105,000.00; bidders listed 1
- **Approved by:** Timkev on 2026-10-01T16:54:55+00:00

## Limits of this review

- The PPRA feed publishes tenders and awards, not the bid documents, so bids themselves were not evaluated.
- Award values are lump sums, so price comparisons point to scope questions, not proven overpricing.
- Records with missing or zero values were excluded and are listed in the audit log.
