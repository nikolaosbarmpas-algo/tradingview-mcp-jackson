# PEAD — Post-Earnings Announcement Drift

A mechanical backtest of the single most-documented anomaly in academic
finance, extracted straight from the research papers — no optimization, no
curve-fitting, no discretion.

> **What is PEAD?** After a company reports earnings, its price keeps *drifting*
> in the direction of the earnings surprise for several weeks. Beat → keeps
> rising. Miss → keeps falling. The number is public the instant it's released,
> yet the price takes ~60 trading days to fully absorb it. That delayed
> reaction is the inefficiency we trade.

## The strategy in three rules

A complete strategy needs an entry, an exit, and position sizing. That's all
this is.

| Component | Rule |
|-----------|------|
| **Entry** | *Concordant* signal — the earnings surprise **and** the price reaction must agree. Beat + up day → **long**. Miss + down day → **short**. Disagreement → no trade. Enter at the **open of the session after** the reaction has played out. |
| **Exit** | Time-based. Hold **60 trading days**, then close. No stop, no target. |
| **Sizing** | **10% of equity** per position on a **$100,000** account. |

Why no stop loss? The drift is *statistical*, not directional — over 60 days a
position can wander well into the red and still finish a winner. A tight stop
would systematically cut the winners and break the edge.

### Timing of the entry

- **After-market-close (AMC)** report → the reaction day is the *next* session
  (the news only becomes tradable at the next open).
- **Before-market-open (BMO)** report → the reaction day is the *same* session.

In both cases we enter at the open of the session *after* the full reaction.
Calling `strategy.entry()` on the report bar fills at the next bar's open, which
reproduces this exactly.

## Configurations

The video tested five configurations. The `Configuration` input lets you switch
between them (plus a `Custom` mode driven by the manual switches).

| # | Concordant filter | Surprise threshold | Side | Verdict |
|---|-------------------|--------------------|------|---------|
| 1 | ✅ on  | — | long + short | By-the-book base case |
| 2 | ❌ off | — | long + short | Surprise direction only — more return, more drawdown |
| 3 | ❌ off | 5% | long + short | High-magnitude surprises only |
| 4 | ✅ on  | 5% | long + short | **Both filters stacked → degrades** |
| 5 | ✅ on  | — | **long only** | **Cleanest risk-adjusted** |

### Reported results (20-stock basket, Jan 2018 → Apr 2026, 658 events)

| Config | Net profit | Max DD | Profit factor | Win rate | Calmar |
|--------|-----------:|-------:|--------------:|---------:|-------:|
| 1 (base)        | ~$131k | 17.4% | 1.99 | 61% | ~0.68 |
| 2 (no filter)   | ~$251k | 32.5% | —    | —   | ~0.69 |
| 3 (5% only)     | ~$191k | 26%   | —    | —   | ~0.67 |
| 4 (both)        | ~$82k  | 20%   | —    | —   | ~0.36 |
| 5 (long only)   | ~$146k | 20%   | 2.68 | 63% | ~0.76 |

Two findings worth keeping:

1. **Stacking the concordant filter and the 5% threshold breaks the strategy**
   (config 4). The two filters do partially overlapping work; combined they
   strip out a small but high-quality set of signals that were carrying the
   edge. Papers often *propose* multiple filters but rarely *test them
   together* — you only see this interaction by running the experiment.
2. **The short leg is consistently a loser** (roughly −$14k to −$19k across
   runs). This isn't a bug — post-2010, positive surprises drift far more
   reliably than negative ones on large caps. Bad news gets pre-announced,
   guided down on the call, and shorted ahead of the print, so most of the
   negative reaction is already priced in by release. Dropping the short leg
   (config 5) gives the cleanest by-the-book version.

> Numbers above are the video's MultiCharts portfolio results across the basket.
> This Pine strategy runs one symbol at a time; per-symbol figures will differ,
> and basket-level numbers require running the same universe (below) and summing
> the equity curves. Treat them as the target, not a guarantee.

## Two implementations

| File | What it is | Best for |
|------|-----------|----------|
| [`pead-strategy.pine`](./pead-strategy.pine) | Pine v6 `strategy()` for the TradingView Strategy Tester | Visual, on-chart, one symbol at a time |
| [`pead_backtest.py`](./pead_backtest.py) | Standalone Python portfolio backtest over the whole basket | Reproducing the basket-level numbers, batch research |

## How to run it (Pine / TradingView)

1. Open the Pine editor in TradingView and paste
   [`pead-strategy.pine`](./pead-strategy.pine).
2. Add it to a **daily** chart of any of the basket symbols.
3. Pick a `Configuration` (default is **#5, long only — the cleanest**).
4. Open the **Strategy Tester** to read net profit, drawdown, profit factor,
   and win rate.

Via this repo's MCP tools:

```
pine_new            → create a blank strategy
pine_set_source     → inject pead-strategy.pine
pine_smart_compile  → compile + auto-detect errors
ui_open_panel       → "strategy-tester" to read the report
chart_set_symbol    → cycle the basket to compare per-symbol results
```

## How to run it (Python)

The Python version backtests the **whole basket at once** and prints the same
metrics the video reports (net P/L, max drawdown, profit factor, win rate,
CAGR, Calmar) plus a **long-vs-short P/L split**. The engine is **pure standard
library** — no dependencies for the offline/self-test paths.

```bash
# 1) Verify the engine end-to-end with synthetic data — zero deps, zero network:
python3 pead_backtest.py --selftest --config all

# 2) Live FREE data (prices + actual/estimate EPS) from Yahoo via yfinance:
pip install -r requirements.txt
python3 pead_backtest.py --source yf --config all --cache data

# 3) Offline / the video's actual workflow — paste data into CSVs and read back:
python3 pead_backtest.py --source csv --data-dir data --config 5 --per-symbol
```

A small **runnable sample** ships under [`data/`](./data) so the `--source csv`
path works out of the box and documents the exact file format:

```
data/prices/<SYMBOL>.csv     date,open,high,low,close,volume   (open & close used)
data/earnings/<SYMBOL>.csv   date,timing,actual_eps,estimate_eps   (timing = BMO/AMC, optional)
```

Useful flags: `--config 1..5|all`, `--hold-days 60`, `--pct 10`,
`--capital 100000`, `--symbols AAPL MSFT ...`, `--per-symbol`.

> **Sizing note:** the Python engine uses fixed-fraction sizing (10% of the
> *initial* capital per position) so trades are order-independent and easy to
> audit. The video's MultiCharts run compounds off current equity, so absolute
> dollar figures will differ — the *shape* of the findings (filter interactions,
> the weak short leg) is what reproduces.

### The 20-stock basket (video universe)

Deliberately mixed across sectors — and including names that *struggled* over
the window (NKE, SBUX, LLY) so the result isn't just "go long things that went
up." These are all large, surviving, heavily-followed companies, which is
PEAD's **hardest** setting: the drift is known to be *stronger* on smaller,
less-covered names where information uncertainty is higher. If it works here, it
should work better on small/mid caps.

```
AAPL  MSFT  GOOGL  AMZN  NVDA   (tech)
NKE   SBUX  MCD    KO            (consumer)
JPM   BAC   GS                   (financial)
LLY   JNJ   PFE                  (healthcare)
CAT   BA    GE                   (industrial)
XOM   CVX                        (energy)
```

(Exact tickers are illustrative of the sector mix described in the video; swap
in whichever liquid names you want to reproduce or extend the test.)

## Earnings data — free, no spreadsheet

The video sources earnings (date, BMO/AMC flag, actual EPS, consensus estimate)
for free from Zacks and pastes it into a sheet. This Pine version skips the
copy-paste entirely: it pulls **actual** and **estimate** EPS live from
TradingView via `request.earnings()`, so the surprise is computed on the fly and
the strategy re-derives every event when you change the chart symbol. Free data,
zero infrastructure — the whole point.

The one thing TradingView's feed doesn't expose cleanly is the BMO/AMC flag. We
sidestep it by measuring the price reaction on the report bar and entering at the
*next* open, which is correct for both cases.

## Where to take it next

This is the *starting point*, not the destination:

- Run it on **less liquid small/mid caps** — the literature says the edge is
  stronger there.
- Test it on **non-US markets** (Europe, Asia).
- **Combine** the PEAD signal with other factors.
- For serious work, move to a proper **cross-sectional** backtest in Python
  rather than one-symbol-at-a-time on a chart.

## The research

PEAD has survived 50+ years of scrutiny and refused to be arbitraged away — a
slow, multi-week repricing driven by gradual institutional model updates,
staggered sell-side revisions, emotional retail reactions, and real structural
limits to arbitrage (idiosyncratic risk, transaction costs, exposure caps).

- **Ball & Brown (1968)** — *An Empirical Evaluation of Accounting Income
  Numbers.* First documented that prices don't fully react on the announcement
  day.
- **Bernard & Thomas (1989)** — *Post-Earnings-Announcement Drift: Delayed Price
  Response or Risk Premium?* Formalized the decile-sort methodology; top-minus-
  bottom spread was positive in 41 of 48 quarters, drift ~60 days.
- **Livnat & Mendenhall (2006)** — *Comparing the Post-Earnings Announcement
  Drift for Surprises Calculated from Analyst and Time Series Forecasts.*
  Defined the modern entry/exit windows still used today.
- **Fink (2021)** — review of 200+ papers across 50+ years: the effect persists
  globally.

---

*Educational reproduction of published research for backtesting. Not investment
advice. Past performance — especially a backtest on a curated basket — does not
predict future results.*
