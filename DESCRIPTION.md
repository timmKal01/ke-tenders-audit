# Ke-Tenders Audit: project description

**Theme:** Value for money (Governance track, The Bid Box Challenge).
**Institution and workflow:** procurement evaluation committees and internal audit units in Kenyan public
bodies (county governments, TVET colleges, schools, state agencies), at the point where awards are
reviewed and a report goes to the committee or the auditor.

Kenyan public bodies publish every tender and award on the PPRA portal in the Open Contracting Data
Standard, but almost nobody has time to read it. Committees and auditors spend weeks checking bidders,
comparing prices and writing reports, while oversight bodies face thousands of records with no way to
see which deserve a second look.

Ke-Tenders Audit is an agentic AI that prepares that review file, then stops. A reviewer asks a question
such as "Review all published awards by PC Kinyanjui Technical Training Institute". The agent, built on
LangGraph and running an open-weights Qwen model, plans the review and calls tools on our own MCP
server: it searches awards, runs integrity checks (single bidder, signed before the tender closed, just
below a round amount, repeat winner at the same buyer), compares prices with similar awards and
profiles suppliers. It reads its own results, recovers from bad data, and proposes flags.

Every proposed flag pauses the run until a named human approves, rejects or requests changes. Code, not
the prompt, refuses flags that cite a record no tool returned, name the wrong supplier, call a normal
price an outlier, or use language that decides an award or declares guilt. The agent never awards a
tender. Each finding in the committee report cites its OCDS record, and a table computed from the data
lists every pattern found, so a wrong model summary cannot hide anything.

The project is built on the tendered-and-awarded half of the data; no bid documents are published.
A full buyer review costs about three US cents on a hosted model, and nothing on a local one.
