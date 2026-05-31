#!/usr/bin/env python3
"""Generate a 10-K "morning note" for one or more tickers from SEC EDGAR.

This is the free, public-data answer to the "Wall Street analyst in 60 seconds"
pitch: instead of paid data connectors (FactSet, Daloopa, S&P), it pulls the
latest annual report (Form 10-K) straight from SEC EDGAR via the open-source
`edgartools` library. No API key, no subscription -- the SEC only asks that you
identify yourself with a name + email.

Usage:
    export EDGAR_IDENTITY="Your Name your@email.com"
    python edgar/morning_note.py AAPL
    python edgar/morning_note.py AAPL MSFT NVDA --filings 8
    python edgar/morning_note.py AAPL --out notes/

Notes:
- Data is point-in-time from the most recent 10-K; it is NOT live price data.
- Statement parsing depends on the filer's XBRL; sections degrade gracefully
  (a missing section prints a note rather than crashing the whole report).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def resolve_identity(cli_identity: str | None) -> str:
    """Return the SEC identity string, or exit with guidance if absent."""
    identity = cli_identity or os.environ.get("EDGAR_IDENTITY")
    if not identity:
        _eprint(
            "ERROR: SEC requires an identity (a name + email).\n"
            "  Set it once:   export EDGAR_IDENTITY='Your Name your@email.com'\n"
            "  Or pass it:    --identity 'Your Name your@email.com'\n"
            "This is an SEC fair-access requirement, not a paid subscription."
        )
        raise SystemExit(2)
    return identity


def _render_statement(statement, max_rows: int | None) -> str:
    """Best-effort markdown for an edgartools Statement object."""
    if statement is None:
        return "_Not available in this filing._"
    for method in ("to_markdown", "to_llm_context"):
        fn = getattr(statement, method, None)
        if callable(fn):
            try:
                text = fn()
                if text:
                    text = str(text).strip()
                    if max_rows is not None:
                        lines = text.splitlines()
                        if len(lines) > max_rows:
                            text = "\n".join(lines[:max_rows]) + "\n_… (truncated)_"
                    return text
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                _eprint(f"  warn: {method}() failed: {exc}")
    try:
        return f"```\n{str(statement).strip()}\n```"
    except Exception:  # noqa: BLE001
        return "_Could not render this statement._"


def _safe(getter, default="—"):
    """Call a zero-arg lambda, swallowing errors into a default."""
    try:
        value = getter()
        return value if value not in (None, "") else default
    except Exception:  # noqa: BLE001
        return default


def _recent_filings_table(company, limit: int) -> str:
    try:
        filings = company.get_filings(form=["10-K", "10-Q", "8-K"])
    except Exception as exc:  # noqa: BLE001
        return f"_Could not load recent filings: {exc}_"

    seen: set[str] = set()
    records: list[tuple[str, str, str]] = []
    for filing in filings:
        accession = _safe(lambda: filing.accession_no, default="")
        if accession and accession in seen:
            continue  # EDGAR can list the same filing more than once
        seen.add(accession)
        form = _safe(lambda: filing.form)
        filed = _safe(lambda: filing.filing_date)
        records.append((str(filed), str(form), str(accession)))

    if not records:
        return "_No recent 10-K/10-Q/8-K filings found._"

    # Most recent first (filing_date is ISO YYYY-MM-DD, so string sort works).
    records.sort(key=lambda r: r[0], reverse=True)

    rows = ["| Form | Filed | Accession |", "| --- | --- | --- |"]
    for filed, form, accession in records[:limit]:
        rows.append(f"| {form} | {filed} | {accession} |")
    return "\n".join(rows)


def build_note(ticker: str, *, filings: int, max_rows: int | None) -> str:
    from edgar import Company  # imported here so --help works without network

    ticker = ticker.upper()
    company = Company(ticker)

    name = _safe(lambda: company.name)
    cik = _safe(lambda: company.cik)
    sic = _safe(lambda: company.sic)
    industry = _safe(lambda: company.industry)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    tenk = company.latest_tenk
    if tenk is None:
        return (
            f"# {ticker} — Morning Note\n\n"
            f"**{name}** (CIK {cik})\n\n"
            "_No 10-K found for this issuer (foreign filers use 20-F)._\n"
        )

    period = _safe(lambda: tenk.period_of_report)
    filed = _safe(lambda: tenk.filing_date)

    # NOTE: edgartools' statement properties are one-shot — the first access on a
    # TenK returns a Statement, repeat/interleaved accesses can return None. So we
    # grab each statement exactly once into a local, cash flow first (the most
    # fragile), and never touch the property again.
    cash_flow = getattr(tenk, "cash_flow_statement", None)
    income = getattr(tenk, "income_statement", None)
    balance = getattr(tenk, "balance_sheet", None)

    parts = [
        f"# {ticker} — Morning Note",
        "",
        f"**{name}**  ·  CIK {cik}  ·  SIC {sic} ({industry})",
        f"_Generated {generated} · Source: SEC EDGAR (Form 10-K)_",
        "",
        f"**Latest 10-K** — filed {filed}, period ending {period}",
        "",
        "## Income Statement",
        _render_statement(income, max_rows),
        "",
        "## Balance Sheet",
        _render_statement(balance, max_rows),
        "",
        "## Cash Flow",
        _render_statement(cash_flow, max_rows),
        "",
        f"## Recent Filings (last {filings})",
        _recent_filings_table(company, filings),
        "",
        "---",
        "_Point-in-time data from the latest annual report. Not live prices, "
        "not investment advice._",
        "",
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 10-K morning note from SEC EDGAR (free, public data)."
    )
    parser.add_argument("tickers", nargs="+", help="One or more tickers, e.g. AAPL MSFT")
    parser.add_argument(
        "--identity",
        help="SEC identity 'Name email@x.com' (else uses $EDGAR_IDENTITY).",
    )
    parser.add_argument(
        "--filings", type=int, default=6, help="How many recent filings to list (default 6)."
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Cap lines per statement to keep notes compact (default: full).",
    )
    parser.add_argument(
        "--out",
        help="Directory to also write each note as <TICKER>.md.",
    )
    args = parser.parse_args(argv)

    identity = resolve_identity(args.identity)

    from edgar import set_identity

    set_identity(identity)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for i, ticker in enumerate(args.tickers):
        try:
            note = build_note(ticker, filings=args.filings, max_rows=args.max_rows)
        except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't kill the run
            failures += 1
            _eprint(f"ERROR building note for {ticker}: {exc}")
            continue

        if i > 0:
            print("\n" + "=" * 72 + "\n")
        print(note)

        if out_dir:
            path = out_dir / f"{ticker.upper()}.md"
            path.write_text(note, encoding="utf-8")
            _eprint(f"  wrote {path}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
