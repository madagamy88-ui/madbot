"""
annotate_outcomes.py — MadBot Phase 5 groundwork

madbot_signals.csv logs every decision analyze() made, but not what price did
afterward. This script closes that gap: for each logged row old enough to have
N bars of history since it fired, it fetches the actual subsequent candles and
records the forward return — so mr_confirms, the SMA200 gate, and every other
hand-tuned threshold can eventually be checked against real outcomes instead
of assumed correct.

Usage:
  python annotate_outcomes.py              # default: 10-bar forward return
  python annotate_outcomes.py --bars 20
  python annotate_outcomes.py --bars 10 --bars 20 --bars 50   # multiple horizons

Output: madbot_signals_enriched.csv (source file is never modified in place).
Idempotent — re-running only fills in rows that are newly old enough or were
previously un-annotatable (e.g. malformed source row); already-annotated rows
are left untouched.

IMPORTANT — this needs live exchange/data access to run for real:
  This was written and syntax-checked in a sandboxed environment with no
  Binance/Indodax/yfinance reachability. Run it on your own machine, where
  analyze.py's fetch_ohlcv() / fetch_stock() already work, before trusting it.
  Verify with the checklist at the bottom of this file.

Fixes 2026-07-22 (both confirmed against real data in madbot_signals_enriched.csv
before being fixed, not just found by code reading):
  1. Asset-type misclassification — "BANK" was missing from analyze.py's static
     CRYPTO_TICKERS set, so it was silently classified as a stock, fetched as
     "BANK.JK" via Finnhub/yfinance, and compared against its real crypto entry
     price. 4 rows had plausible-looking but wrong -75% to -85% forward returns
     as a result, no error raised anywhere. Fixed by using analyze.py's own
     _detect_asset_type() instead of a static-only check — same fast path, but
     falls back to a live Binance probe for anything not in the hardcoded set,
     so this doesn't just fix BANK, it fixes every future dynamically-discovered
     coin the static set hasn't caught up to yet.
  2. Fixed fetch window (limit=300) didn't scale with how old an entry is —
     confirmed 669 of 1223 real rows (54.7%) have an entry-to-check gap over
     300 bars, worst case 547. For those, every fetched bar still satisfied
     ">= entry_ts" (even the oldest fetched bar was younger than a 547-bar-old
     entry), so the existing safety check never fired and .iloc[n_bars] silently
     read from the wrong end of an unrelated window — same silent-wrong-number
     failure shape as bug #1, just a different mechanism. Fixed by sizing the
     fetch window to the entry's actual age, capped near typical exchange
     per-call limits; returns None (not a capped-but-still-wrong window) when a
     single fetch genuinely can't reach back far enough, matching this
     function's existing fail-open convention rather than silently guessing.

  NEITHER FIX retroactively repairs rows already written under the old bugs —
  load_already_enriched() dedupes by (timestamp, ticker, timeframe), so any row
  already in madbot_signals_enriched.csv stays marked "done" forever regardless
  of what the code does going forward. The corrupted rows from both bugs need
  to be identified and removed from the enriched CSV directly; this script will
  not touch or re-process them on its own.

Fixes 2026-07-23 (audit findings beyond the two above; each verified by
actually running the code against synthetic and real data, not just read):
  3. CSV schema drift — out_cols is built from whichever --bars this run
     uses, but the header was only ever written once, on the first run that
     created the file; every later run just appended regardless of whether
     ITS --bars matched what the header already promised. A later run with a
     different horizon set would append rows with a different column count
     than the header declares, silently misaligning every row after the
     drift point for any reader that aligns columns positionally (pandas,
     csv.DictReader) — no exception anywhere. Same failure class that
     already happened once in this codebase: load_source_rows()'s own
     11-vs-12-column handling exists because the SOURCE log went through
     exactly this drift once already. Fixed with _read_existing_header() and
     a check in main() that refuses to append a mismatched schema instead of
     silently corrupting the file. Verified end-to-end: fed it a 13-column
     existing file and a --bars 10 20 50 run (15 columns) — correctly
     refused with a clear error instead of writing the corrupted file.
  4. Fragment-repair heuristic gaps — two distinct issues in
     load_source_rows()'s "no comma = fragment" repair: (a) a fragment that
     is itself the very first body line has nothing to glue onto and was
     silently discarded, uncounted — contradicting this function's own
     "skipped and counted, not silently dropped" guarantee; now counted as
     a bad row. (b) the heuristic only catches a split that happens to leave
     a comma-less remainder — a split landing inside a string field can
     still produce a line with the right field count, passing through
     undetected as a "valid" row with corrupted values. Added a content-
     level sanity check (timestamp must parse, score and entry_price_idr
     must be numeric) to catch this. Verified with a real before/after test:
     fed both the original and fixed code the same row with a garbage score
     field — the original silently accepted it as valid, the fixed version
     correctly rejected it.
  5. Silent row loss with no accounting — two `continue` points in main()'s
     loop (unparseable timestamp, every horizon unresolved) had zero
     counting or logging, unlike load_source_rows() which reports what it
     skips and why. Added date_parse_failures/unresolved_rows counters,
     reported at the end of each run.
  6. `if entry_price:` was a truthy check — a legitimate entry_price of
     exactly 0.0 would be treated identically to "failed to parse," silently
     skipping forward-price computation with no distinguishing signal.
     Changed to `is not None`.
  NOT fixed here, flagged instead: this file's own import-time sys.exit(1)
     (same pattern as the screener.py bug fixed this round) was left alone
     because this script is explicitly a standalone offline tool per its own
     docstring above, not something a Streamlit app would import, so the
     "kills a shared server process" risk that motivated fixing it in
     screener.py doesn't apply here.

Fixes 2026-07-24 (the redundant-fetch item flagged but not fixed on
2026-07-23 — confirmed as the real cause before fixing, not patched blind):
  7. Redundant per-horizon fetching — fetch_forward_price() was called once
     PER HORIZON from main()'s loop (up to 3x per row for --bars 10 20 50),
     each call an independent live network fetch with a slightly different
     `limit`, which almost never share a disk-cache hit with each other.
     Confirmed as the actual cause, not just a plausible theory: TON's 3
     oldest rows (546-583 bars old, correctly classified as crypto, well
     under the fetch-window cap from fix #2) resolved fwd_return_pct_10b but
     left 20b/50b blank across TWO separate runs on different dates — every
     other ticker at a comparably large gap resolved all three horizons
     cleanly in the same runs, ruling out genuine data-availability as the
     explanation. Replaced fetch_forward_price() with fetch_forward_prices()
     (plural) — fetches ONCE per row, sized to the LARGEST requested
     horizon, and slices that one in-memory frame for every horizon. A row's
     horizons now succeed or fail together from one fetch, not three
     independent ones. Each horizon still gets its own "enough forward
     history reached this bar yet" check against the shared frame, so a
     genuinely too-recent horizon still correctly returns None on its own —
     this only removes the redundant network calls, not the legitimate
     per-horizon insufficient-data case. NOT YET re-verified against live
     data (this environment has no exchange access) — verify TON resolves
     cleanly on your next real run before considering this closed.
"""
import argparse
import csv
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

# Reuse the exact fetchers analyze.py already uses — no duplicated network
# logic, no risk of drifting out of sync with how live data is pulled there.
try:
    from analyze import fetch_ohlcv, fetch_stock, fetch_rate, _detect_asset_type, TF_SECONDS
except ImportError as e:
    print(f"ERROR: could not import from analyze.py ({e}). "
          f"Run this from the same folder as analyze.py.", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR   = pathlib.Path(__file__).resolve().parent
SOURCE_LOG   = SCRIPT_DIR / "madbot_signals.csv"
ENRICHED_LOG = SCRIPT_DIR / "madbot_signals_enriched.csv"

EXPECTED_COLS = [
    "timestamp", "ticker", "timeframe", "actual_tf", "score", "bias",
    "regime", "htf", "btc", "confidence", "entry_price_idr", "sma200_above",
]

# SCHEMA EXTENSION (Stage 0, 2026-08-13): analyze.py's logger now appends 8
# more columns (SL/TP levels, counter_trend_valid, the 4 score components) —
# same "11-col -> 12-col" style additive bump this file was already built to
# expect, just one more step. Rows written by the OLD analyze.py stay 11 or
# 12 columns forever (that data is real, not going away); rows written by
# the NEW analyze.py are 20 columns. Both must be readable from the same
# file at once, since the source log is append-only and spans both eras.
EXPECTED_COLS_V2 = EXPECTED_COLS + [
    "sl_raw_idr", "tp1_raw_idr", "tp2_raw_idr", "counter_trend_valid",
    "component_trend", "component_momentum", "component_volume", "component_structure",
]

# The new path-aware output columns — deliberately named nothing like
# fwd_return_pct_Nb, on purpose (design decision made explicit earlier):
# these measure a fundamentally different thing (which level was actually
# touched first) than the fixed-horizon check does (where was price N bars
# later regardless of what happened in between). Keeping them visually and
# nominally distinct stops anyone reading this file later from silently
# blending two different measurement methodologies as if they were one
# consistent series.
PATH_AWARE_COLS = ["outcome_status", "r_multiple", "mae_r", "mfe_r", "bars_to_outcome"]

# How far forward the path-aware walk looks for a resolution before giving
# up and reporting STILL_OPEN, separate from --bars (which only controls
# the OLD fixed-horizon check and is unchanged). 200 bars is a documented,
# tunable assumption — a generous window to let a trade actually resolve —
# not a hard constraint from anywhere else in the pipeline. Still bounded
# by MAX_FETCH_LIMIT inside _fetch_forward_window(), same as everything
# else that fetches forward history.
PATH_AWARE_MAX_BARS = 200


def load_source_rows(path: pathlib.Path) -> list[dict]:
    """Parse madbot_signals.csv defensively — the file has THREE historical
    schemas now (11 cols pre-P2-B, 12 cols after sma200_above was added, 20
    cols after the Stage 0 SL/TP/components logger upgrade) and a known
    corruption pattern (numeric values split across two physical lines by
    concurrent-write races before the logger fix). Rows that can't be
    repaired are skipped and counted, not silently dropped.

    Every row this function returns — 11-, 12-, or 20-column in origin —
    carries the same set of keys (EXPECTED_COLS_V2), so every caller
    downstream can safely do row.get("sl_raw_idr") etc. without checking
    which era a row came from. 11/12-column (legacy) rows simply get None
    for every field that didn't exist yet when they were logged — this is
    what lets compute_path_aware_outcome() cleanly skip legacy rows (no
    SL/TP to walk toward) while the untouched fixed-horizon annotation
    keeps working on them exactly as it always has."""
    if not path.exists():
        print(f"No source log found at {path}", file=sys.stderr)
        return []

    with open(path, encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()

    body = [l for l in raw_lines[1:] if l.strip()]
    fixed_lines, skipped_fragments, bad_rows = [], 0, 0
    for l in body:
        l2 = l.rstrip("\n")
        if "," not in l2:
            # fragment from a pre-fix concurrent-write race — glue onto prior line
            if fixed_lines:
                fixed_lines[-1] += l2
                skipped_fragments += 1
            else:
                # BUG FIX (audit finding, 2026-07-23): this fragment is the
                # very first line in the file — there's nothing to glue it
                # onto, so it used to be silently discarded, uncounted,
                # contradicting this function's own "skipped and counted,
                # not silently dropped" guarantee. Count it as a bad row so
                # it shows up in the summary line below like every other
                # unrecoverable row does.
                bad_rows += 1
            continue
        fixed_lines.append(l2)

    parsed_rows = []
    for line in fixed_lines:
        try:
            fields = next(csv.reader([line]))
        except Exception:
            bad_rows += 1
            continue
        if len(fields) == 11:
            row = dict(zip(EXPECTED_COLS[:-1], fields))
            row["sma200_above"] = None
        elif len(fields) == 12:
            row = dict(zip(EXPECTED_COLS, fields))
        elif len(fields) == 20:
            row = dict(zip(EXPECTED_COLS_V2, fields))
        else:
            bad_rows += 1
            continue
        # Legacy (11/12-col) rows never had these fields — give every row
        # the same key set regardless of which era logged it, defaulting to
        # None, not missing-key errors, for anything downstream that reads
        # row.get("sl_raw_idr") etc.
        for _col in EXPECTED_COLS_V2:
            row.setdefault(_col, None)
        # BUG FIX (audit finding, 2026-07-23): the fragment-repair heuristic
        # above only catches a split that happens to leave a comma-less
        # remainder. A split that lands inside a string field, or anywhere
        # that still leaves a comma-containing remainder, produces a line
        # that parses to exactly 11 or 12 fields — passes the field-count
        # check above — while actually being corrupted. There was no
        # content-level check to catch that; a garbage-but-right-shaped row
        # would silently become a "valid" row with wrong values. This is a
        # cheap type/shape sanity check on the fields that matter most
        # downstream (timestamp must parse, score must be numeric, and
        # entry_price_idr must be numeric if present) — not exhaustive, but
        # catches exactly the failure mode described above.
        try:
            datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            float(row["score"])
            if row.get("entry_price_idr"):
                float(row["entry_price_idr"])
            # SCHEMA EXTENSION (Stage 0): same conditional-numeric check,
            # extended to the new fields. Blank is fine (bearish rows have
            # no SL/TP by design, legacy rows have None) — only a
            # NON-blank-but-non-numeric value indicates real corruption.
            for _numcol in ("sl_raw_idr", "tp1_raw_idr", "tp2_raw_idr",
                             "component_trend", "component_momentum",
                             "component_volume", "component_structure"):
                if row.get(_numcol):
                    float(row[_numcol])
        except (ValueError, KeyError, TypeError):
            bad_rows += 1
            continue
        parsed_rows.append(row)

    if skipped_fragments or bad_rows:
        print(f"  repaired {skipped_fragments} split-line fragments, "
              f"skipped {bad_rows} unrecoverable rows", file=sys.stderr)
    return parsed_rows


def load_already_enriched(path: pathlib.Path) -> set:
    """Return the set of (timestamp, ticker, timeframe) keys already present
    in the enriched output, so re-runs don't refetch/reprocess them."""
    if not path.exists():
        return set()
    seen = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            seen.add((row.get("timestamp"), row.get("ticker"), row.get("timeframe")))
    return seen


def _read_existing_header(path: pathlib.Path) -> list[str] | None:
    """Return the column names in the enriched CSV's header row, or None if
    the file doesn't exist yet.

    BUG FIX (audit finding, 2026-07-23): out_cols is built from whichever
    --bars this run happens to use, but the header is only ever written once
    (on the very first run that creates the file) — every later run just
    appends, regardless of whether ITS --bars matches what the file's header
    already promises. csv.DictWriter writes fields in fieldnames order, not
    by matching them to the existing header text, so a later run with a
    different horizon set (e.g. first run --bars 10, later run
    --bars 10 --bars 20 --bars 50) appends rows with a different column
    count than the header declares — every reader that aligns columns
    positionally (pandas.read_csv, csv.DictReader) silently misreads every
    row from that point on, with no exception anywhere. This has already
    happened once before in this exact codebase: load_source_rows()'s own
    docstring documents the source log going from 11 to 12 columns
    (sma200_above added) and needing a defensive parser retrofitted after
    the fact. This helper exists so main() can catch the same failure class
    here before it happens, instead of after."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        first_line = f.readline().rstrip("\n")
    if not first_line:
        return None
    return next(csv.reader([first_line]))


def bars_have_elapsed(entry_ts: datetime, timeframe: str, n_bars: int) -> bool:
    tf_secs = TF_SECONDS.get(timeframe, 3600)
    needed  = timedelta(seconds=tf_secs * n_bars)
    return (datetime.now(timezone.utc) - entry_ts.replace(tzinfo=timezone.utc)) >= needed


def fetch_forward_prices(ticker: str, timeframe: str, entry_ts: datetime,
                          horizons: list, asset_type: str) -> dict:
    """Fetch OHLCV ONCE per row and return the IDR-denominated close price at
    every requested horizon, as {n_bars: price_or_None}. Caller treats a None
    value as 'not yet knowable', same fail-open convention analyze.py uses
    elsewhere.

    BUG FIX (audit finding, 2026-07-24 — confirmed against real data across
    two separate runs, not just traced from code): this replaces the old
    fetch_forward_price(), which was called once PER HORIZON from main()'s
    loop — for a row 500+ bars old requesting 10/20/50, that's 3 separate
    live network calls per row, each with a slightly different `limit`
    (required_limit scales with n_bars), which almost never share a disk-
    cache hit with each other since fetch_ohlcv()'s cache key includes the
    exact limit value. Confirmed in real data: TON's 3 oldest rows (546-583
    bars old across two runs on different dates, well under the 1000-bar
    cap, correctly classified as crypto) resolved fwd_return_pct_10b but
    left 20b/50b blank both times — every other ticker at a comparably
    large gap resolved all three horizons cleanly in the same runs, so this
    was never a genuine data-availability limit specific to how old the row
    was. Three independent live calls per row means three independent
    chances to hit a transient failure (rate limit, timeout), and a partial
    failure on just the 20b/50b calls while the 10b call happens to succeed
    produces exactly the pattern seen. Fetching once, sized to the LARGEST
    requested horizon, and slicing that one in-memory frame for every
    horizon removes the redundant calls and the independent-failure risk
    entirely — a row's horizons now succeed or fail together, from one
    fetch, not three. Each horizon still gets its own 'enough forward
    history reached this bar yet' check against the shared frame, so a
    genuinely too-recent horizon still correctly returns None on its own —
    this only removes the REDUNDANT network calls, not the legitimate
    per-horizon insufficient-data case.

    BUG FIX (audit finding, 2026-07-08): currency conversion. fetch_ohlcv()
    tags its returned frame with df.attrs["source"] — "binance" (quoted in
    USDT) or "indodax" (quoted in IDR) — and analyze.py itself only
    multiplies by the USDT->IDR rate when source=="binance" (its own line
    1541, `is_global = df.attrs.get("source") == "binance"`). The original
    version of this function never did that check, so for any ticker
    resolved via Binance (the overwhelming majority) the forward price came
    back as a raw USDT number compared directly against an IDR
    entry_price_idr — confirmed by back-solving the implied forward price
    from several rows in the actual enriched CSV and finding it matched a
    plausible USDT price for that asset, not a plausible IDR one.
    fetch_stock()'s two sources (finnhub, yfinance) both already tag
    quote="IDR" — no conversion needed there, matches analyze.py's own
    handling of IDX stocks.

    KNOWN LIMITATION, stated rather than hidden: fetch_rate() returns the
    CURRENT USDT->IDR rate at the time this script runs, not the rate at
    entry_ts or at entry_ts+n_bars. analyze.py has no historical-rate source
    either, so this matches the codebase's existing capability rather than
    introducing a new gap — but it does mean forward-return % will carry a
    small additional error equal to however much USDT/IDR itself moved
    between entry_ts and now. For BTC-quote-pair assets over a period of
    days this is typically a low-single-digit-percent effect — worth
    knowing about, not a reason to distrust the fix. Now fetched at most
    ONCE per row (previously once per horizon, up to 3x) since it doesn't
    depend on which horizon is being computed.
    """
    after, is_usdt_quoted = _fetch_forward_window(ticker, timeframe, entry_ts, asset_type, max(horizons))
    if after is None:
        return {n: None for n in horizons}
    return _fixed_horizon_returns(after, is_usdt_quoted, horizons)


def _fetch_forward_window(ticker: str, timeframe: str, entry_ts: datetime,
                           asset_type: str, max_bars: int):
    """Shared fetch+prep, extracted from fetch_forward_prices() (Stage 0,
    2026-08-13) so the new path-aware TP/SL walk can reuse the SAME fetched
    window instead of hitting the network a second time for the same row.
    This function's body is a straight extraction — the fetch sizing logic,
    the MAX_FETCH_LIMIT guard, the exception handling, and the timestamp
    normalization are all copied verbatim from what fetch_forward_prices()
    already did inline; nothing about how much data gets fetched or when a
    fetch is skipped has changed. fetch_forward_prices() itself now just
    calls this and does its own per-horizon slicing on the result, so its
    external behavior (return shape, values, fail-open cases) is unchanged
    for any existing caller.

    Returns (after_df, is_usdt_quoted), or (None, None) on any failure —
    same fail-open contract fetch_forward_prices() always had.
    """
    fetch_fn = fetch_ohlcv if asset_type == "crypto" else fetch_stock

    entry_ts_aware = entry_ts.replace(tzinfo=timezone.utc) if entry_ts.tzinfo is None else entry_ts
    tf_secs = TF_SECONDS.get(timeframe, 3600)
    bars_since_entry = (datetime.now(timezone.utc) - entry_ts_aware).total_seconds() / tf_secs
    MAX_FETCH_LIMIT = 1000  # typical exchange per-call ceiling (Binance klines caps near here)
    required_limit = int(bars_since_entry) + max_bars + 20  # +20 bars of slack
    if required_limit > MAX_FETCH_LIMIT:
        return None, None  # entry too old for one fetch call to safely cover

    limit = max(required_limit, 50)  # floor so very recent entries still get a sane window

    try:
        result = fetch_fn(ticker, timeframe, limit)
        df = result[0] if isinstance(result, (tuple, list)) else result
        if df is None or df.empty:
            return None, None
    except Exception as e:
        print(f"    fetch failed for {ticker} {timeframe}: {e}", file=sys.stderr)
        return None, None

    is_usdt_quoted = df.attrs.get("source") == "binance"

    if "timestamp" not in df.columns:
        # analyze.py's OHLCV frames are indexed by time in some paths —
        # normalize defensively rather than assume a column name.
        df = df.reset_index().rename(columns={df.index.name or "index": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")

    entry_ts_utc = pd.Timestamp(entry_ts).tz_localize("UTC") if entry_ts.tzinfo is None \
        else pd.Timestamp(entry_ts)
    after = df[df["timestamp"] >= entry_ts_utc].reset_index(drop=True)
    return after, is_usdt_quoted


def _fixed_horizon_returns(after: pd.DataFrame, is_usdt_quoted: bool, horizons: list) -> dict:
    """The per-horizon closing-price extraction, factored out of
    fetch_forward_prices() unchanged so main() can compute this AND the
    path-aware walk from one shared fetch instead of two. Logic is
    byte-identical to what was inline before — see fetch_forward_prices()'s
    docstring above for the currency-conversion and rate-staleness
    reasoning, which still applies exactly as documented there."""
    rate = None  # fetched at most once, lazily, only if a horizon actually needs it
    results = {}
    for n in horizons:
        if len(after) <= n:
            results[n] = None  # not enough forward history fetched for THIS horizon — try again later
            continue
        close_native = float(after["close"].iloc[n])
        if is_usdt_quoted:
            if rate is None:
                rate, _rate_src = fetch_rate()
            results[n] = close_native * rate
        else:
            results[n] = close_native
    return results


def compute_path_aware_outcome(after: pd.DataFrame, is_usdt_quoted: bool,
                                entry_price_idr: float, sl_idr: float,
                                tp1_idr: float, tp2_idr: float) -> dict | None:
    """Walk the forward OHLCV window chronologically, bar by bar, and
    determine which level — SL, TP1, or TP2 — was actually touched FIRST,
    instead of the old fixed-horizon 'where was price N bars later
    regardless of what happened in between' check above. This is what
    actually answers whether a trade would have won or lost; the fixed-
    horizon check above answers a different, still-useful question (does
    the score correlate with direction at all) and is kept exactly as-is,
    not replaced.

    INTRABAR AMBIGUITY — a real, documented limitation, not solved: OHLC
    data cannot tell you the order events happened WITHIN one candle. If a
    single bar's range contains both the SL and a TP level, tick data would
    be needed to know which was actually touched first. This function uses
    the standard, defensible convention: SL wins on an ambiguous bar. Every
    bar is checked in priority order SL > TP2 > TP1 — the worse outcome on
    any tie, never the better one.

    Only meaningful for a real long setup (sl_idr below entry_price_idr) —
    returns None for anything else (missing inputs, malformed levels, an
    empty window), which the caller treats as 'not computable for this
    row', same fail-open convention as everywhere else in this file. This
    is also what naturally excludes bearish rows: size_position() never
    returns real SL/TP for score<=-2 (long-only system), so those rows
    simply have nothing here to walk toward — not an error, just N/A.

    Returns a dict with:
      outcome_status  — "HIT_SL" | "HIT_TP1" | "HIT_TP2" | "STILL_OPEN"
      r_multiple      — realized R on exit (always exactly -1.0 for
                         HIT_SL by construction), or None if STILL_OPEN
      mae_r           — Maximum Adverse Excursion, in R, the worst
                         unrealized drawdown reached before exit (or
                         through the fetched window, if still open)
      mfe_r           — Maximum Favorable Excursion, in R, the best
                         unrealized gain reached before exit
      bars_to_outcome — bar index the outcome triggered on, or bars
                         walked so far if STILL_OPEN
    """
    if after is None or after.empty:
        return None
    if not entry_price_idr or not sl_idr or entry_price_idr <= sl_idr:
        return None  # no real long setup to walk (or malformed inputs)

    risk_unit = entry_price_idr - sl_idr  # always positive for a real long setup
    rate = None
    mae_r, mfe_r = 0.0, 0.0
    outcome, exit_r, bars_to_outcome = "STILL_OPEN", None, len(after) - 1

    for i, bar in enumerate(after.itertuples()):
        bar_low, bar_high = float(bar.low), float(bar.high)
        if is_usdt_quoted:
            if rate is None:
                rate, _rate_src = fetch_rate()
            bar_low *= rate
            bar_high *= rate

        # Track excursion for EVERY bar walked, including the exit bar
        # itself, before checking whether this bar ends the trade — a
        # closed trade's excursion includes the bar it closed on.
        mae_r = min(mae_r, (bar_low - entry_price_idr) / risk_unit)
        mfe_r = max(mfe_r, (bar_high - entry_price_idr) / risk_unit)

        if bar_low <= sl_idr:
            outcome, exit_r, bars_to_outcome = "HIT_SL", -1.0, i
            break
        if tp2_idr and bar_high >= tp2_idr:
            outcome, exit_r, bars_to_outcome = "HIT_TP2", (tp2_idr - entry_price_idr) / risk_unit, i
            break
        if tp1_idr and bar_high >= tp1_idr:
            outcome, exit_r, bars_to_outcome = "HIT_TP1", (tp1_idr - entry_price_idr) / risk_unit, i
            break

    return {
        "outcome_status":  outcome,
        "r_multiple":      round(exit_r, 3) if exit_r is not None else None,
        "mae_r":           round(mae_r, 3),
        "mfe_r":           round(mfe_r, 3),
        "bars_to_outcome": bars_to_outcome,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", type=int, action="append", default=None,
                     help="Forward horizon(s) in bars. Repeatable. Default: 10.")
    ap.add_argument("--limit", type=int, default=None,
                     help="Process at most N eligible rows this run (rate-limit friendly).")
    args = ap.parse_args()
    horizons = args.bars or [10]

    rows = load_source_rows(SOURCE_LOG)
    if not rows:
        print("Nothing to annotate.")
        return
    already = load_already_enriched(ENRICHED_LOG)

    fwd_cols = [f"fwd_return_pct_{n}b" for n in horizons]
    # SCHEMA EXTENSION (Stage 0): out_cols now includes the new source
    # fields (SL/TP/components — EXPECTED_COLS_V2 vs the old EXPECTED_COLS)
    # and the new path-aware columns. This is a real, deliberate schema
    # bump — the existing schema-drift guard right below will correctly
    # refuse to append to an enriched CSV written under the OLD shape
    # rather than silently misaligning it. That refusal is expected and
    # CORRECT the first time this version runs against an old enriched
    # file — see this file's own docstring / the run notes for what to do
    # when you hit it (move or delete the old enriched CSV and let it
    # regenerate from the full source log; nothing about the source log
    # itself is affected either way).
    out_cols = EXPECTED_COLS_V2 + fwd_cols + PATH_AWARE_COLS + ["outcome_checked_at"]

    # BUG FIX (audit finding, 2026-07-23): refuse to append rows whose column
    # count doesn't match what's already on disk. See _read_existing_header()
    # docstring for the full failure mechanics this prevents.
    existing_header = _read_existing_header(ENRICHED_LOG)
    if existing_header is not None and existing_header != out_cols:
        print(
            f"ERROR: {ENRICHED_LOG.name} already exists with header:\n"
            f"  {existing_header}\n"
            f"but this run (--bars {horizons}) would write rows shaped like:\n"
            f"  {out_cols}\n"
            f"Refusing to append a mismatched schema — every row after the "
            f"drift point would silently misalign when read back.\n"
            f"Either rerun with the same --bars this file was originally "
            f"created with, or move/delete {ENRICHED_LOG.name} and let it "
            f"regenerate from scratch with the new --bars.",
            file=sys.stderr,
        )
        sys.exit(1)

    write_header = existing_header is None
    processed = 0
    # BUG FIX (audit finding, 2026-07-23): these two failure points used to
    # `continue` with zero counting or logging — load_source_rows() reports
    # what it skips and why, but rows lost here (unparseable timestamp, or
    # every horizon unresolved) vanished with no trace, so there was no way
    # to tell how many rows were being silently eaten at this later stage,
    # including any that reached here corrupted-but-well-shaped from the
    # load_source_rows() fragment-repair heuristic.
    date_parse_failures = 0
    unresolved_rows = 0
    with open(ENRICHED_LOG, "a", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=out_cols)
        if write_header:
            writer.writeheader()

        for row in rows:
            key = (row["timestamp"], row["ticker"], row["timeframe"])
            if key in already:
                continue
            try:
                entry_ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                date_parse_failures += 1
                continue

            max_bars = max(horizons)
            if not bars_have_elapsed(entry_ts, row["actual_tf"] or row["timeframe"], max_bars):
                continue  # too recent — not enough forward history exists yet

            if args.limit and processed >= args.limit:
                break

            # BUG FIX (2026-07-22, confirmed against real data — "BANK" was
            # missing from the static CRYPTO_TICKERS set, causing it to be
            # silently misclassified as a stock, fetched via Finnhub/yfinance
            # as "BANK.JK", and compared against its real crypto entry price
            # — 4 rows in the enriched CSV had plausible-looking but wrong
            # -75% to -85% forward returns as a result, no error raised).
            # _detect_asset_type() already exists in analyze.py for exactly
            # this: same static-set fast path, but falls back to a live
            # Binance ticker probe for anything not in the set, so a newly
            # discovered coin resolves correctly instead of deterministically
            # misclassifying. Only fails open to "stock" on a genuine network
            # error during that probe, not on every unlisted-coin call.
            asset_type = _detect_asset_type(row["ticker"])
            try:
                entry_price = float(row["entry_price_idr"])
            except (TypeError, ValueError):
                entry_price = None

            out_row = dict(row)
            any_price = False
            # BUG FIX (audit finding, 2026-07-24): was a per-horizon loop
            # calling fetch_forward_price() once for each of horizons — now
            # one call up front gets every horizon from a single fetch.
            #
            # STAGE 0 EXTENSION (2026-08-13): that single fetch is now ALSO
            # shared with the new path-aware TP/SL walk below, sized to
            # whichever needs more bars (the fixed horizons or
            # PATH_AWARE_MAX_BARS) — still exactly one network call per
            # row, not two. fetch_forward_prices() itself is left fully
            # intact above for any other caller; main() just calls the
            # same two pieces (_fetch_forward_window +
            # _fixed_horizon_returns) it's built from directly, so this row
            # loop can reuse the fetched window for the path-aware step
            # too instead of going through the wrapper twice.
            fwd_prices = {}
            path_aware = None
            # BUG FIX (audit finding, 2026-07-23): was `if entry_price:`,
            # a truthy check — a legitimate entry_price of exactly 0.0
            # (shouldn't happen for a real asset, but would if an
            # upstream logging bug ever produced one) would be treated
            # identically to "failed to parse", silently skipping this
            # row with no distinguishing signal. is not None is the
            # correct check for "did we get a value at all".
            if entry_price is not None:
                max_bars_needed = max(max(horizons), PATH_AWARE_MAX_BARS)
                after, is_usdt_quoted = _fetch_forward_window(
                    row["ticker"], row["actual_tf"] or row["timeframe"],
                    entry_ts, asset_type, max_bars_needed,
                )
                if after is not None:
                    fwd_prices = _fixed_horizon_returns(after, is_usdt_quoted, horizons)

                    # Path-aware only computable for rows that actually
                    # have real SL/TP logged — new-schema (20-col) BULLISH
                    # rows. Legacy (11/12-col) rows and bearish rows
                    # (size_position() never returns real SL/TP for
                    # score<=-2 — long-only system) correctly have nothing
                    # here to walk toward. Fail-open, not an error: this
                    # is exactly the boundary EXPECTED_COLS_V2's
                    # row.setdefault(_col, None) was built to make clean.
                    try:
                        sl_v  = float(row["sl_raw_idr"])  if row.get("sl_raw_idr")  else None
                        tp1_v = float(row["tp1_raw_idr"]) if row.get("tp1_raw_idr") else None
                        tp2_v = float(row["tp2_raw_idr"]) if row.get("tp2_raw_idr") else None
                    except (TypeError, ValueError):
                        sl_v = tp1_v = tp2_v = None
                    if sl_v is not None:
                        path_aware = compute_path_aware_outcome(
                            after, is_usdt_quoted, entry_price, sl_v, tp1_v, tp2_v
                        )

            for n in horizons:
                fwd_price = fwd_prices.get(n)
                if fwd_price is not None:
                    out_row[f"fwd_return_pct_{n}b"] = round(
                        (fwd_price - entry_price) / entry_price * 100, 3
                    )
                    any_price = True
                else:
                    out_row[f"fwd_return_pct_{n}b"] = ""

            if path_aware:
                out_row.update(path_aware)
                # STILL_OPEN is real, useful information but not a final
                # resolution — only let it force this row to be WRITTEN
                # this run if a fixed horizon also resolved (any_price
                # already True from above). A genuinely resolved path-aware
                # outcome (HIT_SL/HIT_TP1/HIT_TP2) counts on its own,
                # exactly like a resolved fixed horizon already does.
                if path_aware["outcome_status"] != "STILL_OPEN":
                    any_price = True
            else:
                for _c in PATH_AWARE_COLS:
                    out_row[_c] = ""

            if not any_price:
                unresolved_rows += 1
                continue  # couldn't resolve anything — leave for next run

            out_row["outcome_checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow({c: out_row.get(c, "") for c in out_cols})
            processed += 1
            _path_tag = (f"  [{out_row['outcome_status']} r={out_row['r_multiple']}]"
                         if path_aware else "")
            print(f"  annotated {row['ticker']:6s} {row['timestamp']} "
                  f"score={row['score']:>3}  " +
                  "  ".join(f"{n}b={out_row[f'fwd_return_pct_{n}b']}%" for n in horizons) +
                  _path_tag)

    print(f"\nDone. {processed} new rows annotated -> {ENRICHED_LOG.name}")
    if date_parse_failures or unresolved_rows:
        print(f"  {date_parse_failures} rows skipped this run (unparseable timestamp), "
              f"{unresolved_rows} rows left for a future run (no horizon resolved yet)",
              file=sys.stderr)


if __name__ == "__main__":
    main()

# ── Verification checklist (run these yourself — no live data access here) ──
#   1. py annotate_outcomes.py --limit 5
#      -> should print a handful of "annotated TICKER ..." lines with a
#         fwd_return_pct_10b value, not blank, not a crash.
#   2. Open madbot_signals_enriched.csv — confirm it has all the original
#      columns PLUS fwd_return_pct_10b and outcome_checked_at.
#   3. Re-run with no --limit — rows already in the enriched file should be
#      skipped (no duplicates), confirmed by row count not doubling.
#   4. Sanity-check one row by hand: pick a ticker/timestamp, look up its
#      entry_price_idr, then check what that ticker's price actually was
#      ~10 bars later on your own chart. The sign and rough magnitude of
#      fwd_return_pct_10b should match what you see.
#   5. Once a few days of this has accumulated: this is the file to bring
#      back for the mr_confirms / SMA200-gate calibration conversation —
#      it's the piece that was missing.
#   6. Schema guard: run once with --bars 10, then again with
#      --bars 10 --bars 20 --bars 50 on the SAME enriched file without
#      deleting it — the second run should refuse with a clear error
#      instead of appending a mismatched-width row. Delete the test file
#      and let it regenerate to confirm normal operation still works.
