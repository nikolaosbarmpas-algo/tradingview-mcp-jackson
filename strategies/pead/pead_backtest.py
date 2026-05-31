#!/usr/bin/env python3
"""
PEAD — Post-Earnings Announcement Drift  ·  Python backtest
===========================================================

A mechanical, by-the-book backtest of finance's most-documented anomaly: after
an earnings report, a stock keeps drifting in the direction of the surprise for
~60 trading days. We trade that drift with three rules and nothing else.

    1. ENTRY  — concordant signal: the earnings surprise AND the same-day price
                reaction must agree. Beat + up day -> long. Miss + down day ->
                short. Disagreement -> no trade. Enter at the OPEN of the
                session after the reaction has played out.
    2. EXIT   — time-based. Hold `hold_days` (default 60) trading days, then
                close. No stop, no target (the drift is statistical).
    3. SIZING — fixed % of capital per position (default 10%) on a $100k account.

Five configurations from the research/video are reproduced (see CONFIGS).

------------------------------------------------------------------------------
Data — free, three ways
------------------------------------------------------------------------------
  --source yf    Pull prices + earnings (actual EPS vs. consensus estimate)
                 live and free from Yahoo via `yfinance`. Optionally cached to
                 CSV for reproducible offline reruns. (Needs `pip install
                 yfinance`; the rest of the program is pure standard library.)

  --source csv   The video's actual workflow: paste data into spreadsheets and
                 read it back. Expects, per symbol:
                   <data_dir>/prices/<SYMBOL>.csv     date,open,high,low,close,volume
                   <data_dir>/earnings/<SYMBOL>.csv   date,timing,actual_eps,estimate_eps
                 `timing` is BMO / AMC (before/after market) — optional; if
                 missing we fall back to a timestamp heuristic, else assume AMC.

  --selftest     Run on deterministic synthetic data with a built-in drift, so
                 you can verify the engine end-to-end with zero dependencies and
                 zero network.

Examples
--------
  python3 pead_backtest.py --selftest
  python3 pead_backtest.py --source yf --config all --cache data
  python3 pead_backtest.py --source csv --data-dir data --config 5 --per-symbol

Educational reproduction of published research. Not investment advice.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# -----------------------------------------------------------------------------
# Default universe — the video's 20-stock, multi-sector basket. Deliberately
# includes names that struggled over the window (NKE, SBUX, LLY had drawdowns)
# so the result isn't just "go long things that went up", and is all large,
# heavily-covered companies — PEAD's HARDEST setting (the drift is stronger on
# small, less-followed names).
# -----------------------------------------------------------------------------
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",   # tech
    "NKE", "SBUX", "MCD", "KO",                # consumer
    "JPM", "BAC", "GS",                        # financial
    "LLY", "JNJ", "PFE",                       # healthcare
    "CAT", "BA", "GE",                         # industrial
    "XOM", "CVX",                              # energy
]


# =============================================================================
# Configuration
# =============================================================================
@dataclass(frozen=True)
class Config:
    name: str
    concordant: bool      # require the same-day reaction to agree with the surprise
    use_threshold: bool   # filter out small-magnitude surprises
    min_surprise_pct: float
    long_only: bool


# The five configurations tested in the video.
CONFIGS = {
    "1": Config("1 - Concordant, no threshold, long+short (base)", True,  False, 5.0, False),
    "2": Config("2 - Surprise only, no threshold, long+short",     False, False, 5.0, False),
    "3": Config("3 - Surprise only, 5% threshold, long+short",     False, True,  5.0, False),
    "4": Config("4 - Concordant + 5% threshold, long+short",       True,  True,  5.0, False),
    "5": Config("5 - Concordant, no threshold, LONG ONLY (clean)", True,  False, 5.0, True),
}


# =============================================================================
# Data model
# =============================================================================
@dataclass
class Bar:
    d: date
    open: float
    close: float


@dataclass
class Earning:
    d: date                 # report calendar date
    actual: float           # reported EPS
    estimate: float         # consensus estimate
    timing: str = ""        # "BMO" / "AMC" / "" (unknown)


@dataclass
class SymbolData:
    symbol: str
    bars: list[Bar]
    earnings: list[Earning]
    _idx: dict[date, int] = field(default_factory=dict)

    def build_index(self) -> None:
        self.bars.sort(key=lambda b: b.d)
        self.earnings.sort(key=lambda e: e.d)
        self._idx = {b.d: i for i, b in enumerate(self.bars)}

    def first_idx_on_or_after(self, day: date) -> int | None:
        for i, b in enumerate(self.bars):
            if b.d >= day:
                return i
        return None

    def first_idx_strictly_after(self, day: date) -> int | None:
        for i, b in enumerate(self.bars):
            if b.d > day:
                return i
        return None

    def close_on_or_before(self, day: date) -> float | None:
        """Forward-filled close (handles cross-symbol calendar gaps)."""
        prev = None
        for b in self.bars:
            if b.d > day:
                break
            prev = b.close
        return prev


# =============================================================================
# Trade
# =============================================================================
@dataclass
class Trade:
    symbol: str
    side: int               # +1 long, -1 short
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: float
    surprise_pct: float

    @property
    def pnl(self) -> float:
        return self.side * self.shares * (self.exit_price - self.entry_price)

    @property
    def notional(self) -> float:
        return self.shares * self.entry_price


# =============================================================================
# Signal generation (one symbol, one config)
# =============================================================================
def _reaction_index(sd: SymbolData, e: Earning) -> int | None:
    """
    Index of the 'reaction bar' — the first session that digests the news.
    BMO (before open): the report day itself is the reaction day.
    AMC (after close): the next session is the reaction day.
    Unknown timing -> assume AMC (the conservative default for large caps).
    """
    timing = (e.timing or "").upper()
    if timing == "BMO":
        return sd.first_idx_on_or_after(e.d)
    # AMC or unknown
    return sd.first_idx_strictly_after(e.d)


def generate_trades(sd: SymbolData, cfg: Config, hold_days: int,
                    capital: float, pct: float) -> list[Trade]:
    trades: list[Trade] = []
    n = len(sd.bars)
    notional_per_trade = capital * pct / 100.0  # fixed-fraction sizing

    for e in sd.earnings:
        if e.estimate is None or e.actual is None:
            continue
        surprise = e.actual - e.estimate
        if surprise == 0:
            continue  # no edge in a perfectly in-line print

        denom = abs(e.estimate)
        surprise_pct = (surprise / denom * 100.0) if denom != 0 else math.copysign(999.0, surprise)

        if cfg.use_threshold and abs(surprise_pct) < cfg.min_surprise_pct:
            continue

        r = _reaction_index(sd, e)
        if r is None or r < 1 or r + 1 >= n:
            continue  # need a previous close, and a next bar to enter on

        reaction_up = sd.bars[r].close > sd.bars[r - 1].close
        reaction_down = sd.bars[r].close < sd.bars[r - 1].close

        beat = surprise > 0
        miss = surprise < 0

        long_sig = beat and (not cfg.concordant or reaction_up)
        short_sig = miss and (not cfg.concordant or reaction_down) and not cfg.long_only
        if not (long_sig or short_sig):
            continue

        side = 1 if long_sig else -1

        entry_idx = r + 1                      # enter at the next session's open
        exit_idx = min(entry_idx + hold_days, n - 1)  # 60-day hold (cap at data end)
        if exit_idx <= entry_idx:
            continue

        entry_price = sd.bars[entry_idx].open
        exit_price = sd.bars[exit_idx].open
        if entry_price <= 0:
            continue

        shares = notional_per_trade / entry_price
        trades.append(Trade(
            symbol=sd.symbol, side=side,
            entry_date=sd.bars[entry_idx].d, exit_date=sd.bars[exit_idx].d,
            entry_price=entry_price, exit_price=exit_price,
            shares=shares, surprise_pct=surprise_pct,
        ))
    return trades


# =============================================================================
# Portfolio metrics (daily mark-to-market across the basket)
# =============================================================================
@dataclass
class Result:
    config: str
    capital: float
    trades: list[Trade]
    net_profit: float
    return_pct: float
    max_dd_pct: float
    profit_factor: float
    win_rate: float
    cagr_pct: float
    calmar: float
    long_pnl: float
    short_pnl: float
    n_long: int
    n_short: int


def _equity_curve(trades: list[Trade], sym_data: dict[str, SymbolData],
                  capital: float) -> list[tuple[date, float]]:
    """Daily portfolio equity = capital + realized + unrealized, across all symbols."""
    if not trades:
        return []
    start = min(t.entry_date for t in trades)
    end = max(t.exit_date for t in trades)

    # Union of trading days in [start, end] from the symbols we actually traded.
    used = {t.symbol for t in trades}
    days: set[date] = set()
    for sym in used:
        for b in sym_data[sym].bars:
            if start <= b.d <= end:
                days.add(b.d)
    calendar = sorted(days)

    curve: list[tuple[date, float]] = []
    for day in calendar:
        eq = capital
        for t in trades:
            if day < t.entry_date:
                continue
            if day >= t.exit_date:
                eq += t.pnl                                   # realized
            else:
                px = sym_data[t.symbol].close_on_or_before(day)  # unrealized
                if px is not None:
                    eq += t.side * t.shares * (px - t.entry_price)
        curve.append((day, eq))
    return curve


def evaluate(config_name: str, trades: list[Trade], sym_data: dict[str, SymbolData],
             capital: float) -> Result:
    net = sum(t.pnl for t in trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0

    long_pnl = sum(t.pnl for t in trades if t.side > 0)
    short_pnl = sum(t.pnl for t in trades if t.side < 0)
    n_long = sum(1 for t in trades if t.side > 0)
    n_short = sum(1 for t in trades if t.side < 0)

    curve = _equity_curve(trades, sym_data, capital)
    max_dd_pct = 0.0
    if curve:
        peak = curve[0][1]
        for _, eq in curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak * 100.0
                max_dd_pct = max(max_dd_pct, dd)

    if curve:
        span_days = (curve[-1][0] - curve[0][0]).days
        years = max(span_days / 365.25, 1e-9)
        final_eq = capital + net
        cagr_pct = ((final_eq / capital) ** (1.0 / years) - 1.0) * 100.0 if final_eq > 0 else -100.0
    else:
        cagr_pct = 0.0

    calmar = (cagr_pct / max_dd_pct) if max_dd_pct > 0 else float("inf")

    return Result(
        config=config_name, capital=capital, trades=trades,
        net_profit=net, return_pct=net / capital * 100.0,
        max_dd_pct=max_dd_pct, profit_factor=profit_factor, win_rate=win_rate,
        cagr_pct=cagr_pct, calmar=calmar,
        long_pnl=long_pnl, short_pnl=short_pnl, n_long=n_long, n_short=n_short,
    )


# =============================================================================
# Reporting
# =============================================================================
def print_table(results: list[Result]) -> None:
    hdr = (f"{'Config':<46}{'Net P/L':>12}{'Ret%':>8}{'MaxDD%':>8}"
           f"{'PF':>7}{'Win%':>7}{'CAGR%':>8}{'Calmar':>8}{'Trades':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        pf = "inf" if math.isinf(r.profit_factor) else f"{r.profit_factor:.2f}"
        cal = "inf" if math.isinf(r.calmar) else f"{r.calmar:.2f}"
        print(f"{r.config:<46}{r.net_profit:>12,.0f}{r.return_pct:>8.1f}"
              f"{r.max_dd_pct:>8.1f}{pf:>7}{r.win_rate:>7.1f}"
              f"{r.cagr_pct:>8.1f}{cal:>8}{len(r.trades):>8}")
    print()
    for r in results:
        print(f"  {r.config}")
        print(f"      long  : {r.long_pnl:>12,.0f}  ({r.n_long} trades)")
        print(f"      short : {r.short_pnl:>12,.0f}  ({r.n_short} trades)"
              + ("   <- note: short leg drags on large caps post-2010" if r.short_pnl < 0 else ""))


def print_per_symbol(cfg: Config, sym_data: dict[str, SymbolData],
                     hold_days: int, capital: float, pct: float) -> None:
    print(f"\nPer-symbol breakdown — {cfg.name}")
    print(f"{'Symbol':<8}{'Net P/L':>12}{'Trades':>8}{'Win%':>8}")
    print("-" * 36)
    for sym in sorted(sym_data):
        trades = generate_trades(sym_data[sym], cfg, hold_days, capital, pct)
        net = sum(t.pnl for t in trades)
        wins = sum(1 for t in trades if t.pnl > 0)
        wr = (wins / len(trades) * 100.0) if trades else 0.0
        print(f"{sym:<8}{net:>12,.0f}{len(trades):>8}{wr:>8.1f}")


# =============================================================================
# Data loading
# =============================================================================
def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    # ISO with time / tz
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def load_csv(symbol: str, data_dir: str) -> SymbolData:
    p_path = os.path.join(data_dir, "prices", f"{symbol}.csv")
    e_path = os.path.join(data_dir, "earnings", f"{symbol}.csv")
    bars: list[Bar] = []
    with open(p_path, newline="") as f:
        for row in csv.DictReader(f):
            row = {k.lower().strip(): v for k, v in row.items()}
            bars.append(Bar(_parse_date(row["date"]),
                            float(row["open"]), float(row["close"])))
    earnings: list[Earning] = []
    if os.path.exists(e_path):
        with open(e_path, newline="") as f:
            for row in csv.DictReader(f):
                row = {k.lower().strip(): v for k, v in row.items()}
                act, est = row.get("actual_eps", ""), row.get("estimate_eps", "")
                if act in ("", "nan", "None") or est in ("", "nan", "None"):
                    continue
                earnings.append(Earning(_parse_date(row["date"]), float(act), float(est),
                                        (row.get("timing") or "").strip()))
    sd = SymbolData(symbol, bars, earnings)
    sd.build_index()
    return sd


def _col(row, *names):
    """Tolerant column lookup across yfinance versions (case / spacing differ)."""
    norm = {str(k).lower().replace(" ", ""): k for k in row.index}
    for n in names:
        key = n.lower().replace(" ", "")
        if key in norm:
            return row[norm[key]]
    return None


def fetch_yf(symbol: str, start: str, end: str, limit: int) -> SymbolData:
    import yfinance as yf  # lazy import — only needed for the online path

    t = yf.Ticker(symbol)
    # auto_adjust=True -> split/dividend-adjusted OHLC, so 60-day holds aren't
    # broken by artificial split jumps.
    hist = t.history(start=start, end=end, auto_adjust=True)
    bars = [Bar(idx.date(), float(row["Open"]), float(row["Close"]))
            for idx, row in hist.iterrows()
            if not (math.isnan(row["Open"]) or math.isnan(row["Close"]))]

    earnings: list[Earning] = []
    try:
        ed = t.get_earnings_dates(limit=limit)
    except Exception:  # noqa: BLE001 — some tickers have no earnings calendar
        ed = None
    if ed is not None:
        for idx, row in ed.iterrows():
            actual = _col(row, "Reported EPS", "epsActual")
            estimate = _col(row, "EPS Estimate", "epsEstimate")
            try:
                actual, estimate = float(actual), float(estimate)
            except (TypeError, ValueError):
                continue
            if math.isnan(actual) or math.isnan(estimate):
                continue  # skip future / unreported dates
            ts = idx.to_pydatetime()
            # Yahoo encodes the session in the timestamp hour: pre-open -> BMO,
            # late afternoon -> AMC. Fall back to "" (engine assumes AMC).
            hour = ts.hour
            timing = "BMO" if 0 < hour < 12 else ("AMC" if hour >= 16 else "")
            earnings.append(Earning(ts.date(), actual, estimate, timing))

    sd = SymbolData(symbol, bars, earnings)
    sd.build_index()
    return sd


def cache_to_csv(sd: SymbolData, data_dir: str) -> None:
    os.makedirs(os.path.join(data_dir, "prices"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "earnings"), exist_ok=True)
    with open(os.path.join(data_dir, "prices", f"{sd.symbol}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "close"])
        for b in sd.bars:
            w.writerow([b.d.isoformat(), b.open, b.close])
    with open(os.path.join(data_dir, "earnings", f"{sd.symbol}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "timing", "actual_eps", "estimate_eps"])
        for e in sd.earnings:
            w.writerow([e.d.isoformat(), e.timing, e.actual, e.estimate])


# =============================================================================
# Self-test — deterministic synthetic data with a built-in drift
# =============================================================================
def make_synthetic(symbol: str, seed: int) -> SymbolData:
    rng = random.Random(seed)
    bars: list[Bar] = []
    earnings: list[Earning] = []
    d = date(2018, 1, 1)
    price = 100.0
    drift = 0.0           # active post-earnings drift, decays over the hold
    drift_left = 0
    next_earn = 40 + rng.randint(0, 20)
    i = 0
    while d <= date(2026, 4, 30):
        if d.weekday() < 5:  # weekday => trading day
            # Schedule an earnings event roughly quarterly.
            if i == next_earn:
                estimate = round(rng.uniform(0.5, 3.0), 2)
                # 60% beats (post-2010 asymmetry favouring the long side).
                surprise = rng.uniform(0.02, 0.30) if rng.random() < 0.60 else -rng.uniform(0.02, 0.30)
                actual = round(estimate + surprise, 2)
                earnings.append(Earning(d, actual, estimate, "AMC"))
                # The market reacts the next day and then drifts the same way.
                drift = math.copysign(0.0015, surprise)   # ~0.15%/day drift
                drift_left = 60
                next_earn = i + 60 + rng.randint(0, 8)

            shock = rng.gauss(0, 0.012)
            day_ret = shock + (drift if drift_left > 0 else 0.0)
            if drift_left > 0:
                drift_left -= 1
            open_p = price
            close_p = max(1.0, price * (1.0 + day_ret))
            bars.append(Bar(d, round(open_p, 2), round(close_p, 2)))
            price = close_p
            i += 1
        d += timedelta(days=1)

    sd = SymbolData(symbol, bars, earnings)
    sd.build_index()
    return sd


# =============================================================================
# Main
# =============================================================================
def build_dataset(args) -> dict[str, SymbolData]:
    sym_data: dict[str, SymbolData] = {}
    if args.selftest:
        universe = args.symbols or ["SYN1", "SYN2", "SYN3", "SYN4", "SYN5"]
        for k, sym in enumerate(universe):
            sym_data[sym] = make_synthetic(sym, seed=1000 + k)
        return sym_data

    universe = args.symbols or DEFAULT_UNIVERSE
    for sym in universe:
        try:
            if args.source == "yf":
                sd = fetch_yf(sym, args.start, args.end, args.earn_limit)
                if args.cache:
                    cache_to_csv(sd, args.cache)
            else:
                sd = load_csv(sym, args.data_dir)
        except Exception as exc:  # noqa: BLE001 — keep going across the basket
            print(f"  ! skipping {sym}: {exc}", file=sys.stderr)
            continue
        if len(sd.bars) < 2 or not sd.earnings:
            print(f"  ! skipping {sym}: insufficient data "
                  f"({len(sd.bars)} bars, {len(sd.earnings)} earnings)", file=sys.stderr)
            continue
        sym_data[sym] = sd
    return sym_data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="PEAD post-earnings announcement drift backtest.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["yf", "csv"], default="csv",
                    help="Data source (default: csv). 'yf' needs `pip install yfinance`.")
    ap.add_argument("--selftest", action="store_true",
                    help="Run on deterministic synthetic data (no deps, no network).")
    ap.add_argument("--symbols", nargs="*", help="Override the symbol universe.")
    ap.add_argument("--data-dir", default="data", help="CSV root for --source csv.")
    ap.add_argument("--cache", default="", help="Cache fetched data to this dir (--source yf).")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2026-05-01")
    ap.add_argument("--earn-limit", type=int, default=60, help="Max earnings dates to pull (yf).")
    ap.add_argument("--config", default="all",
                    help="Configuration: 1-5, or 'all' (default).")
    ap.add_argument("--hold-days", type=int, default=60)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--pct", type=float, default=10.0, help="Percent of capital per position.")
    ap.add_argument("--per-symbol", action="store_true",
                    help="Also print a per-symbol breakdown (uses --config, or config 5).")
    args = ap.parse_args(argv)

    print("Loading data ...", file=sys.stderr)
    sym_data = build_dataset(args)
    if not sym_data:
        print("No usable symbol data. For --source csv, populate "
              "<data_dir>/prices/<SYM>.csv and <data_dir>/earnings/<SYM>.csv, "
              "or try --selftest.", file=sys.stderr)
        return 1

    n_events = sum(len(s.earnings) for s in sym_data.values())
    print(f"Universe: {len(sym_data)} symbols, {n_events} earnings events.\n", file=sys.stderr)

    config_keys = list(CONFIGS) if args.config == "all" else [args.config]
    results: list[Result] = []
    for key in config_keys:
        if key not in CONFIGS:
            print(f"Unknown config '{key}' (use 1-5 or all).", file=sys.stderr)
            return 2
        cfg = CONFIGS[key]
        all_trades: list[Trade] = []
        for sd in sym_data.values():
            all_trades.extend(generate_trades(sd, cfg, args.hold_days, args.capital, args.pct))
        results.append(evaluate(cfg.name, all_trades, sym_data, args.capital))

    print(f"PEAD backtest  ·  ${args.capital:,.0f} capital  ·  {args.pct:.0f}% per position  "
          f"·  {args.hold_days}-day hold\n")
    print_table(results)

    if args.per_symbol:
        cfg = CONFIGS[args.config] if args.config in CONFIGS else CONFIGS["5"]
        print_per_symbol(cfg, sym_data, args.hold_days, args.capital, args.pct)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
