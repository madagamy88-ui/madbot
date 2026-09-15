"""
MadBot Trade Ledger (Pure Python Storage Layer)
================================================================================
This module handles all CSV reading, writing, file-locking, and state resolution.
It contains ZERO Streamlit dependencies. 
"""

import os
import time
import csv
import pathlib
from datetime import datetime, timezone
import pandas as pd

from analyze import fetch_ohlcv, fetch_stock, fetch_rate, CRYPTO_CAP, STOCK_CAP

TRADES_LOG = pathlib.Path(__file__).resolve().parent / "madbot_trades_2.csv"
MAX_FETCH_LIMIT = 1000 # Strictly enforced Binance API ceiling

# BUG FIX (audit finding, 2026-09): this pool ceiling is the SAME 10-20%
# per-trade cap analyze.py's size_position() already uses — imported directly
# from analyze.py rather than re-declared, so the two files can never drift
# out of sync with each other. MAX_ALLOC_PCT mirrors size_position()'s own
# btc_bias-dependent 0.10/0.20 split; since btc_bias isn't logged per trade
# (a separate, disclosed gap — see get_committed_capital() docstring), this
# uses the more permissive 0.20 uniformly, which means the cap enforced here
# is the LOOSEST realistic case, not the tightest. Verify against your own
# risk tolerance before relying on this as a hard ceiling.
MAX_ALLOC_PCT = 0.20

# BUG FIX (audit finding, 2026-09): NO fee or slippage was modeled anywhere
# in this file before this — every r_multiple/mae_r/mfe_r value assumed a
# perfectly frictionless fill at the exact logged price. Real round-trip
# spot fees on Indonesian exchanges researched at the time of this fix
# ranged from ~0.2% (best case, flat maker/taker) to ~0.8%+ (USDT-pair,
# taker-both-legs, with Indonesian VAT/income-tax pass-through included) —
# a wide, genuinely uncertain range, not a single confirmed number. THIS
# CONSTANT IS A PLACEHOLDER — verify the actual figure against your own
# exchange account's fee page (which overrides any externally-researched
# number) before trusting NET_R figures for a real capital decision.
# Expressed as a fraction of ENTRY PRICE (not of R), matching how exchanges
# actually charge — see _fee_as_r() for the conversion into R-multiple units.
ROUND_TRIP_FEE_PCT = float(os.environ.get("ROUND_TRIP_FEE_PCT", 0.004))  # 0.4% placeholder — VERIFY

# Upgraded to include quantitative excursion tracking
_TRADE_COLS = [
    "trade_id", "logged_at", "ticker", "timeframe", "asset_type",
    "trade_source",  
    "entry_price_idr", "sl_raw_idr", "tp1_raw_idr", "tp2_raw_idr",
    # BUG FIX (audit finding, 2026-09): position_size_idr is the actual raw
    # dollar/IDR amount committed to this trade at open — previously not
    # recorded anywhere (analyze.py's size_position() only ever returned a
    # formatted display STRING for this, see that file's own fix). Without
    # this, no aggregate exposure check across concurrently open positions
    # was possible; get_committed_capital() below is what actually needed it.
    "position_size_idr",
    "score", "regime", "confidence", "counter_trend_valid",
    "component_trend", "component_momentum", "component_volume", "component_structure",
    "status", "closed_at", "exit_price_idr", "exit_reason",
    "r_multiple", "mae_r", "mfe_r", "bars_to_outcome",
    # BUG FIX (audit finding, 2026-09): r_multiple above is GROSS — no fee
    # deducted. net_r_multiple is the fee-adjusted figure and is what should
    # be used for any real expectancy/equity decision. Both are kept, not
    # one replacing the other, so the raw gross figure stays auditable.
    "net_r_multiple",
    "last_checked_at" 
]

# BUG FIX (audit finding, 2026-09): the exact 27-column schema this file
# used before position_size_idr/net_r_multiple were added. Needed for
# _migrate_schema_if_needed() to parse OLD rows correctly by field count,
# the same len(fields)==N branching pattern annotate_outcomes.py's
# load_source_rows() already uses for ITS multi-schema source log — this
# file never had the equivalent guard for its OWN log, which is exactly
# what let a real corruption happen (confirmed by direct reproduction:
# appending a 29-column row under an unchanged 27-column header produced a
# file pandas.read_csv cannot parse at all — ParserError: "Expected 27
# fields in line 310, saw 29" — which load_trades()'s existing bare
# `except Exception: return empty DataFrame` then silently swallowed,
# making every trade invisible to every reader without ever raising an
# error anywhere).
_OLD_TRADE_COLS_27 = [
    "trade_id", "logged_at", "ticker", "timeframe", "asset_type",
    "trade_source",
    "entry_price_idr", "sl_raw_idr", "tp1_raw_idr", "tp2_raw_idr",
    "score", "regime", "confidence", "counter_trend_valid",
    "component_trend", "component_momentum", "component_volume", "component_structure",
    "status", "closed_at", "exit_price_idr", "exit_reason",
    "last_checked_at", "r_multiple", "mae_r", "mfe_r", "bars_to_outcome",
]

# BUG FIX (audit finding, 2026-09, CONFIRMED by forensic reconstruction
# against a real corrupted file): the constant above originally had
# last_checked_at as the LAST item, matching what a much earlier reviewed
# copy of this file showed. The TRUE historical order — verified by
# content-matching every field against known-valid value patterns (regime
# names, status strings, the exact known-corrupted epoch value from the
# very first audit of this project) across multiple real rows — actually
# has last_checked_at positioned right after exit_reason, with
# r_multiple/mae_r/mfe_r/bars_to_outcome following it. The wrong order
# above caused the schema migration to mislabel every field from
# last_checked_at onward for every pre-existing row, corrupting status,
# exit_reason, and r_multiple across 308 real rows. Fixed here; see
# _migrate_schema_if_needed()'s new intermediate-width branch below for
# the additional real-world case (a 28-field row: the 27 true values plus
# one already-appended blank position_size_idr placeholder from an
# intermediate version of load_trades()'s backward-compat shim) that
# actually caused the corruption in practice — this file was never
# genuinely a clean 27-field file by the time migration ran on it.
_OLD_TRADE_COLS_28_WITH_POSITION_SIZE = _OLD_TRADE_COLS_27 + ["position_size_idr"]


def _migrate_schema_if_needed():
    """
    BUG FIX (audit finding, 2026-09, CONFIRMED by direct reproduction, not
    just theorized): reads the file's raw header and compares it against
    the CURRENT _TRADE_COLS. If they already match, returns immediately —
    this is a cheap check (one line read) on every call, not a full
    re-migration every time.

    If they don't match, repairs the file LINE BY LINE rather than via
    pd.read_csv() — deliberately, because the exact failure this fixes
    means the file may ALREADY contain a mix of old (27-col) and new
    (29-col) rows (confirmed: this happens the moment even one trade gets
    logged after an upgrade, before this fix existed), and pandas cannot
    parse that mixed-width file at all. Each line is matched against
    whichever known column count it has — old 27, current N, or the
    file's own existing header width, in that order — and normalized to
    the full current _TRADE_COLS shape, backfilling any newly-added
    column as blank for rows that predate it. Unparseable lines are
    counted and dropped, not silently discarded with no trace, matching
    load_source_rows()'s "skipped and counted" convention elsewhere in
    this codebase. Rewritten atomically (temp file + os.replace), same
    pattern already used by close_trade()/update_last_checked() here.
    """
    if not TRADES_LOG.exists() or os.path.getsize(TRADES_LOG) == 0:
        return
    with open(TRADES_LOG, encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()
    if not raw_lines:
        return

    try:
        existing_header = next(csv.reader([raw_lines[0].rstrip("\n")]))
    except Exception:
        existing_header = None
    if existing_header == _TRADE_COLS:
        return  # already current — nothing to do, file untouched

    lock = _trades_lock_acquire()
    if lock is None:
        return  # couldn't get the lock — leave it for the next call to retry
    try:
        repaired_rows = []
        bad_rows = 0
        for line in raw_lines[1:]:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                fields = next(csv.reader([line]))
            except Exception:
                bad_rows += 1
                continue
            if len(fields) == len(_TRADE_COLS):
                row = dict(zip(_TRADE_COLS, fields))
            elif len(fields) == len(_OLD_TRADE_COLS_27):
                row = dict(zip(_OLD_TRADE_COLS_27, fields))
            elif len(fields) == len(_OLD_TRADE_COLS_28_WITH_POSITION_SIZE):
                # BUG FIX (audit finding, 2026-09): this is the case that
                # ACTUALLY occurred in production and caused real data
                # corruption — confirmed by forensic reconstruction against
                # a real corrupted file, not theorized. An intermediate
                # version of load_trades()'s backward-compat shim had
                # already appended one blank "position_size_idr" placeholder
                # to old rows before this migration function existed, so by
                # the time migration ran, these rows were 28 fields wide,
                # not a clean 27. Checked explicitly, ahead of the
                # existing_header fallback below, because existing_header
                # is read from the file's own (possibly already-corrupted)
                # first line and is less trustworthy than this known,
                # verified shape.
                row = dict(zip(_OLD_TRADE_COLS_28_WITH_POSITION_SIZE, fields))
            elif existing_header and len(fields) == len(existing_header):
                row = dict(zip(existing_header, fields))
            else:
                bad_rows += 1
                continue
            for col in _TRADE_COLS:
                row.setdefault(col, "")
            repaired_rows.append(row)

        df = pd.DataFrame(repaired_rows, columns=_TRADE_COLS)
        temp_log = TRADES_LOG.with_suffix(".csv.tmp")
        df.to_csv(temp_log, index=False)
        os.replace(temp_log, TRADES_LOG)
        msg = (f"[trade_ledger] Migrated {TRADES_LOG.name}: {len(df)} row(s) "
               f"repaired to current {len(_TRADE_COLS)}-column schema.")
        if bad_rows:
            msg += f" {bad_rows} row(s) unrecoverable and dropped."
        print(msg)
    finally:
        _trades_lock_release(lock)

def _trades_lock_acquire(timeout: float = 3.0, stale_after: float = 15.0):
    lock_path = TRADES_LOG.with_suffix(".csv.lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                return None
            time.sleep(0.02)

def _trades_lock_release(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass

def load_trades() -> pd.DataFrame:
    if not TRADES_LOG.exists():
        return pd.DataFrame(columns=_TRADE_COLS)
    # BUG FIX (audit finding, 2026-09): runs before the read below, not
    # after — this is the actual fix. Cheap on the common case (file
    # already migrated: one header-line read, then returns immediately).
    _migrate_schema_if_needed()
    try:
        df = pd.read_csv(TRADES_LOG, dtype=str).fillna("")
        # Backwards compatibility for older ledgers
        for col in _TRADE_COLS:
            if col not in df.columns:
                df[col] = ""
        return df
    except Exception:
        return pd.DataFrame(columns=_TRADE_COLS)

def get_committed_capital(asset_type: str) -> float:
    """
    BUG FIX (audit finding, 2026-09): this function did not exist — nothing
    in this codebase ever summed how much capital was already committed to
    OPEN positions before opening another one. Confirmed via real ledger
    data: peak concurrency was 20 open crypto positions against a 20%-per-
    trade cap that only supports 5 before exceeding 100% of pool.

    Returns the sum of position_size_idr across every currently-OPEN trade
    of this asset_type. Depends on position_size_idr being populated at
    open time (see log_trade_open()) — rows logged before this fix will
    have a blank position_size_idr and contribute 0 here, understating
    real historical exposure for any pre-fix OPEN row still open when this
    ships. Fails open (returns 0.0) on any parse error, same convention as
    every other fail-open gate in this codebase — a corrupt row here should
    not crash the caller, but see the docstring note in log_trade_open()
    about what fail-open costs you specifically for a capital gate.
    """
    df = load_trades()
    open_t = df[(df["status"] == "OPEN") & (df["asset_type"] == asset_type)]
    total = 0.0
    for v in open_t.get("position_size_idr", []):
        try:
            if v not in ("", None):
                total += float(v)
        except (TypeError, ValueError):
            continue
    return total


def _fee_as_r(entry_price_idr: float, risk_idr: float) -> float:
    """
    Converts ROUND_TRIP_FEE_PCT (a % of entry notional, matching how
    exchanges actually charge) into R-multiple units, so it can be
    subtracted directly from a gross r_multiple. Returns 0.0 on bad input
    (fail-open — a missing fee figure should degrade to "unmodeled", same
    as the gross-only behavior before this fix, not crash the caller).
    """
    try:
        if not risk_idr or risk_idr <= 0:
            return 0.0
        return (ROUND_TRIP_FEE_PCT * entry_price_idr) / risk_idr
    except (TypeError, ZeroDivisionError):
        return 0.0


def log_trade_open(result: dict, ticker: str, timeframe: str, trade_source: str = "MANUAL") -> bool:
    sig   = result.get("signal", {}) or {}
    tr    = result.get("trade_setup", {}) or {}
    comps = sig.get("components", {}) or {}
    now   = datetime.now(timezone.utc)

    asset_type = result.get("asset_type", "")
    money_idr  = tr.get("money_raw_idr")

    # BUG FIX (audit finding, 2026-09): THE portfolio exposure cap. Enforced
    # here, not only in the caller (madbot_engine.py's pick loop), so every
    # current and future call site — the automated engine AND app.py's
    # manual "Log This Trade" button — is protected by a single source of
    # truth instead of each caller needing to remember its own pre-check.
    # Fails OPEN (allows the trade) if money_raw_idr is missing — e.g. a
    # caller on an older analyze.py before that fix shipped — matching this
    # codebase's existing fail-open convention elsewhere, but note plainly:
    # fail-open on a CAPITAL gate means an unfixed analyze.py silently
    # disables this protection rather than blocking on it. Verify
    # analyze.py's size_position() fix is deployed before relying on this.
    if money_idr not in (None, ""):
        try:
            money_idr = float(money_idr)
            pool = CRYPTO_CAP if asset_type == "crypto" else STOCK_CAP
            committed = get_committed_capital(asset_type)
            if committed + money_idr > pool * MAX_ALLOC_PCT * 5:
                # 5x the single-trade cap == 100% of pool at the loosest
                # (20%) per-trade sizing — i.e. this rejects once the pool
                # is genuinely full, not on the first trade past one slot.
                print(f"[trade_ledger] REJECTED {ticker}: committed capital "
                      f"{committed:,.0f} + this trade {money_idr:,.0f} would "
                      f"exceed {asset_type} pool ({pool:,.0f} IDR). "
                      f"Not opening — insufficient free capital.")
                return False
        except (TypeError, ValueError):
            pass  # fail open — see docstring note above

    trade_id = f"{ticker}_{now.strftime('%Y%m%d%H%M%S%f')}"
    
    row = {
        "trade_id":  trade_id,
        "logged_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker, "timeframe": timeframe,
        "asset_type": asset_type,
        "trade_source": trade_source,
        "entry_price_idr": tr.get("entry_raw_idr", ""),
        "sl_raw_idr":  tr.get("sl_raw_idr", ""),
        "tp1_raw_idr": tr.get("tp1_raw_idr", ""),
        "tp2_raw_idr": tr.get("tp2_raw_idr", ""),
        "position_size_idr": tr.get("money_raw_idr", ""),
        "score": sig.get("score", ""), "regime": sig.get("regime", ""),
        "confidence": sig.get("confidence", ""),
        "counter_trend_valid": sig.get("counter_trend_valid", False),
        "component_trend":     comps.get("trend", ""),
        "component_momentum":  comps.get("momentum", ""),
        "component_volume":    comps.get("volume", ""),
        "component_structure": comps.get("structure", ""),
        "status": "OPEN", "closed_at": "", "exit_price_idr": "", "exit_reason": "",
        "r_multiple": "", "mae_r": "", "mfe_r": "", "bars_to_outcome": "",
        "net_r_multiple": "",
        "last_checked_at": ""
    }
    lock = _trades_lock_acquire()
    if lock is None:
        return False
    try:
        write_hdr = not TRADES_LOG.exists() or os.path.getsize(TRADES_LOG) == 0
        with open(TRADES_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_TRADE_COLS)
            if write_hdr:
                w.writeheader()
            w.writerow(row)
        return True
    finally:
        _trades_lock_release(lock)

def close_trade(trade_id: str, exit_price_idr: float, exit_reason: str, 
                r_mult: float = 0.0, mae_r: float = 0.0, mfe_r: float = 0.0, 
                bars_to_outcome: int = 0, net_r_mult: float | None = None) -> bool:
    lock = _trades_lock_acquire()
    if lock is None:
        return False
    try:
        df = load_trades()
        if df.empty or trade_id not in df["trade_id"].values:
            return False
        idx = df.index[df["trade_id"] == trade_id][0]
        df.loc[idx, "status"]         = "CLOSED"
        df.loc[idx, "closed_at"]      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        df.loc[idx, "exit_price_idr"] = str(exit_price_idr)
        df.loc[idx, "exit_reason"]    = exit_reason
        df.loc[idx, "r_multiple"]     = f"{r_mult:.2f}"
        df.loc[idx, "mae_r"]          = f"{mae_r:.2f}"
        df.loc[idx, "mfe_r"]          = f"{mfe_r:.2f}"
        df.loc[idx, "bars_to_outcome"] = str(bars_to_outcome)
        # BUG FIX (audit finding, 2026-09): fee-adjusted net R, stored
        # alongside (not instead of) the gross r_multiple above. Optional
        # param, defaults to None -> blank, so any OTHER existing caller of
        # close_trade() that hasn't been updated to pass this still works
        # unchanged (additive, not a breaking signature change in practice).
        df.loc[idx, "net_r_multiple"] = f"{net_r_mult:.2f}" if net_r_mult is not None else ""
        
        temp_log = TRADES_LOG.with_suffix(".csv.tmp")
        df.to_csv(temp_log, index=False)
        os.replace(temp_log, TRADES_LOG)
        return True
    finally:
        _trades_lock_release(lock)

def update_last_checked(trade_ids: list):
    """Safely writes a human-readable timestamp to open trades to reset the 5-min throttle."""
    lock = _trades_lock_acquire()
    if not lock:
        return
    try:
        df = load_trades()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for tid in trade_ids:
            if tid in df["trade_id"].values:
                idx = df.index[df["trade_id"] == tid][0]
                df.loc[idx, "last_checked_at"] = now_str
                
        temp_log = TRADES_LOG.with_suffix(".csv.tmp")
        df.to_csv(temp_log, index=False)
        os.replace(temp_log, TRADES_LOG)
    finally:
        _trades_lock_release(lock)

def check_and_update_open_trades():
    """
    Silently walks forward OHLCV history for all OPEN trades to check if SL/TP was hit.
    Calculates MAE, MFE, and R-Multiples accurately.
    """
    df = load_trades()
    open_trades = df[df["status"] == "OPEN"]
    if open_trades.empty:
        return

    rate = None
    checked_ids = []
    # BUG FIX (audit finding, 2026-09): this loop had two `continue` points
    # (bad numeric fields, fetch failure) with zero counting or logging —
    # the exact "silent row loss with no accounting" pattern already found
    # and fixed once in this codebase (annotate_outcomes.py, fix #5) but
    # never ported here. Counted and printed at the end of this function,
    # same convention as that fix.
    parse_failures = 0
    fetch_failures = 0
    
    for _, t in open_trades.iterrows():
        last_chk_str = t.get("last_checked_at", "")
        if last_chk_str:
            try:
                chk_ts = pd.Timestamp(last_chk_str)
                if chk_ts.tzinfo is None: chk_ts = chk_ts.tz_localize("UTC")
                if (pd.Timestamp.now(tz="UTC") - chk_ts).total_seconds() < 300:
                    continue
            except Exception:
                pass

        trade_id = t["trade_id"]
        
        try:
            entry = float(t["entry_price_idr"])
            sl = float(t["sl_raw_idr"])
            tp1 = float(t["tp1_raw_idr"])
            tp2 = float(t["tp2_raw_idr"]) if t["tp2_raw_idr"] else None
            
            parsed_ts = pd.Timestamp(t["logged_at"])
            if parsed_ts.tzinfo is None:
                logged_ts = parsed_ts.tz_localize("UTC")
            else:
                logged_ts = parsed_ts.tz_convert("UTC")
                
        except (ValueError, TypeError, KeyError):
            parse_failures += 1
            continue

        asset_type = t.get("asset_type", "crypto")
        fetch_fn = fetch_ohlcv if asset_type == "crypto" else fetch_stock
        
        try:
            res = fetch_fn(t["ticker"], t["timeframe"], limit=MAX_FETCH_LIMIT)
            ohlcv = res[0] if isinstance(res, (tuple, list)) else res
            if ohlcv is None or ohlcv.empty:
                fetch_failures += 1
                continue
        except Exception:
            fetch_failures += 1
            continue
            
        is_usdt = ohlcv.attrs.get("source") == "binance"
        if "timestamp" not in ohlcv.columns:
            ohlcv = ohlcv.reset_index().rename(columns={ohlcv.index.name or "index": "timestamp"})
        ohlcv["timestamp"] = pd.to_datetime(ohlcv["timestamp"], utc=True, errors="coerce")
        
        oldest_candle_ts = ohlcv["timestamp"].iloc[0]
        if logged_ts < oldest_candle_ts:
            # Trade is too old to resolve in a single fetch. Fail open and avoid infinity loops.
            checked_ids.append(trade_id)
            continue
            
        after = ohlcv[ohlcv["timestamp"] >= logged_ts]
        if after.empty:
            checked_ids.append(trade_id)
            continue
        
        trade_closed = False
        mae = entry
        mfe = entry
        bars_elapsed = 0
        exit_price = 0.0
        reason = ""
        
        for _, bar in after.iterrows():
            bars_elapsed += 1
            bar_low, bar_high = float(bar["low"]), float(bar["high"])
            if is_usdt:
                if rate is None: rate, _ = fetch_rate()
                bar_low *= rate
                bar_high *= rate

            if bar_low < mae: mae = bar_low
            if bar_high > mfe: mfe = bar_high

            if bar_low <= sl:
                exit_price = sl
                reason = "LOSS - SL HIT"
                trade_closed = True
                break
            elif tp2 and bar_high >= tp2:
                exit_price = tp2
                reason = "WIN - TP2 HIT"
                trade_closed = True
                break
            elif tp1 and bar_high >= tp1:
                exit_price = tp1
                reason = "WIN - TP1 HIT"
                trade_closed = True
                break

        if trade_closed:
            risk = entry - sl if entry != sl else 1.0
            r_mult = (exit_price - entry) / risk
            mae_r = (mae - entry) / risk
            mfe_r = (mfe - entry) / risk
            # BUG FIX (audit finding, 2026-09): net_r is r_mult minus the
            # round-trip fee, expressed in the same R units. See
            # ROUND_TRIP_FEE_PCT's own comment for why this is a verify-
            # before-trusting placeholder, not a confirmed figure.
            net_r = r_mult - _fee_as_r(entry, risk)
            close_trade(trade_id, exit_price, reason, r_mult, mae_r, mfe_r, bars_elapsed, net_r_mult=net_r)
        else:
            checked_ids.append(trade_id)

    if checked_ids:
        update_last_checked(checked_ids)

    if parse_failures or fetch_failures:
        print(f"[trade_ledger] check_and_update_open_trades: "
              f"{parse_failures} row(s) skipped (unparseable fields), "
              f"{fetch_failures} row(s) skipped (fetch failed) this run.")