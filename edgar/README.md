# EDGAR Morning Note

A free, public-data alternative to the paid "financial analysis" plugins: it
pulls the latest annual report (**Form 10-K**) straight from **SEC EDGAR** and
prints a structured "morning note" — income statement, balance sheet, cash
flow, and recent filings.

No API key. No paid connector (FactSet / Daloopa / S&P). The SEC only requires
that you identify yourself with a name + email (fair-access policy).

## Why this exists

The popular "Claude pulls the 10-K automatically" demos conflate two things:

1. The official Anthropic financial plugins ship **only paid** data connectors
   — there is no SEC EDGAR connector. They do **not** fetch 10-Ks for free.
2. Claude *can* fetch a public 10-K on its own, but that is best-effort web
   retrieval (may grab the wrong year, miss tables/footnotes).

This script gives you the reliable middle path: structured 10-K fundamentals
parsed from EDGAR's XBRL, for free, reproducibly.

## Setup

```bash
pip install -r edgar/requirements.txt
export EDGAR_IDENTITY="Your Name your@email.com"   # SEC fair-access requirement
```

## Usage

```bash
# Single ticker
python edgar/morning_note.py AAPL

# Several at once, list 8 recent filings each
python edgar/morning_note.py AAPL MSFT NVDA --filings 8

# Keep statements compact and also save to files
python edgar/morning_note.py AAPL --max-rows 25 --out notes/
```

| Flag | Meaning |
| --- | --- |
| `--identity` | SEC identity string (else uses `$EDGAR_IDENTITY`) |
| `--filings N` | How many recent 10-K/10-Q/8-K filings to list (default 6) |
| `--max-rows N` | Cap lines per statement to keep the note short |
| `--out DIR` | Also write each note to `DIR/<TICKER>.md` |

## Notes & limitations

- Data is **point-in-time** from the most recent 10-K — not live prices.
- US domestic filers only; foreign issuers file 20-F (not handled here).
- Statement quality depends on the filer's XBRL; missing sections degrade
  gracefully instead of crashing the run.
- Not investment advice.
