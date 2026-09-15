"""
MadBot Autonomous Engine (Phase 2 - Production Build)
================================================================================
Runs silently in the background. Features atomic Mutex concurrency locking,
System32 path immunity, and Stale Trade tagging for offline gaps > 1000 candles.
"""

# ── 1. NATIVE IMPORTS ONLY (Must not fail) ───────────────────────────────────
import os
import sys
import time
import logging
import traceback
from datetime import datetime, timezone

# ── 2. SYSTEM32 IMMUNITY & DIRECTORY RESOLUTION ──────────────────────────────
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
sys.path.insert(0, script_dir)

# ── 3. HEADLESS LOGGING (MUST HAPPEN BEFORE 3RD PARTY IMPORTS) ───────────────
log_file = os.path.join(script_dir, "madbot_engine.log")
logger = logging.getLogger("MadBotEngine")
logger.setLevel(logging.INFO)

if not logger.handlers:
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

# Guarantee that ANY crash in the script writes directly to the log file
def global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("UNCAUGHT FATAL EXCEPTION:", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

# ── 4. SAFE 3RD-PARTY IMPORTS ────────────────────────────────────────────────
try:
    import pandas as pd
    from trade_ledger import check_and_update_open_trades, log_trade_open, load_trades, _TRADE_COLS, TRADES_LOG, _trades_lock_acquire, _trades_lock_release
    from screener import screen, load_config
    from analyze import analyze
except Exception as e:
    # If the wrong Python environment is used by Task Scheduler, it logs it here!
    logger.critical(f"FATAL IMPORT ERROR: Ensure Task Scheduler is using the correct Python venv. Details: {e}")
    sys.exit(1)

def flag_stale_trades():
    """Identifies trades older than the Binance 1000-candle limit and tags them STALE."""
    lock = _trades_lock_acquire()
    if not lock:
        logger.error("Failed to acquire CSV lock for stale trade check.")
        return

    try:
        df = load_trades()
        open_trades = df[df["status"] == "OPEN"]
        if open_trades.empty: 
            return

        stale_found = False
        now_utc = pd.Timestamp.now(tz="UTC")
        # BUG FIX (audit finding, 2026-09): same "silent row loss with no
        # accounting" pattern as trade_ledger.py's matching fix — a bare
        # `except Exception: continue` here previously discarded parse
        # failures with zero visibility.
        parse_failures = 0

        for idx, t in open_trades.iterrows():
            tf = t.get("timeframe", "1H")
            tf_mins = {"1m":1, "5m":5, "15m":15, "1H":60, "4H":240, "1D":1440, "1W":10080}.get(tf, 60)
            
            max_offline_mins = 1000 * tf_mins
            
            try:
                parsed_ts = pd.Timestamp(t["logged_at"])
                logged_ts = parsed_ts.tz_localize("UTC") if parsed_ts.tzinfo is None else parsed_ts.tz_convert("UTC")
                
                mins_elapsed = (now_utc - logged_ts).total_seconds() / 60.0
                if mins_elapsed > max_offline_mins:
                    df.loc[idx, "status"] = "STALE - MANUAL REVIEW"
                    df.loc[idx, "exit_reason"] = f"Downtime exceeded {max_offline_mins/60:.1f}hrs limit."
                    stale_found = True
                    logger.warning(f"Flagged {t['ticker']} as STALE (Exceeded 1000 candle lookup limit).")
            except Exception:
                parse_failures += 1
                continue

        if parse_failures:
            logger.warning(f"flag_stale_trades: {parse_failures} row(s) skipped (unparseable logged_at).")

        if stale_found:
            temp_log = TRADES_LOG.with_suffix(".csv.tmp")
            df.to_csv(temp_log, index=False)
            os.replace(temp_log, TRADES_LOG)
    except Exception as e:
        logger.error(f"Error checking for stale trades: {e}")
    finally:
        _trades_lock_release(lock)

def run_engine_cycle():
    logger.info("--- MADBOT ENGINE CYCLE STARTED ---")
    
    flag_stale_trades()
    
    try:
        check_and_update_open_trades()
        logger.info("Ledger resolution complete.")
    except Exception as e:
        logger.error(f"Error resolving open trades: {e}")

    try:
        cfg = load_config()
        target_asset = cfg.get("default_asset", "crypto")
        logger.info(f"Running neural scan on {target_asset.upper()}...")
        out_data = screen(target_asset, cfg, force_refresh=True, diagnostic=False)
    except Exception as e:
        logger.error(f"Error during market scan: {e}")
        return

    if not out_data or not out_data.get("picks"):
        logger.info("Scan complete: No valid setups detected.")
        return

    picks = out_data["picks"]
    logger.info(f"Scan complete: {len(picks)} setups found.")

    try:
        trades_df = load_trades()
        open_sys = trades_df[(trades_df["status"] == "OPEN") & (trades_df["trade_source"] == "SYSTEM")] if not trades_df.empty else pd.DataFrame(columns=_TRADE_COLS)
    except Exception as e:
        logger.error(f"Failed to load ledger for duplication check: {e}")
        return

    for p in picks:
        p_ticker = p.get("ticker")
        p_tf = out_data.get("timeframe", "1H")
        
        if not open_sys.empty and not open_sys[(open_sys["ticker"] == p_ticker) & (open_sys["timeframe"] == p_tf)].empty:
            logger.info(f"Skipping {p_ticker} — active systemic position already exists.")
            continue
            
        try:
            full_res = analyze(p_ticker, p_tf, asset_type=target_asset, force_refresh=True, log_signal=False)
            tr = full_res.get("trade_setup", {})
            
            if tr and not tr.get("error") and tr.get("entry_raw_idr"):
                success = log_trade_open(full_res, p_ticker, p_tf, trade_source="SYSTEM")
                if success:
                    logger.info(f"SUCCESS: Systemic trade logged for {p_ticker}.")
                else:
                    # BUG FIX (audit finding, 2026-09): False now has two
                    # real causes, not one — a CSV lock timeout, OR the new
                    # exposure-cap check in log_trade_open() rejecting the
                    # trade because the pool is already fully committed.
                    # trade_ledger.py prints the specific reason via
                    # stdout/stderr when it rejects for capital; this log
                    # line is intentionally left generic rather than
                    # guessing which cause applies — check the printed
                    # rejection message (captured wherever this process's
                    # stdout goes) for the specific reason.
                    logger.warning(f"Did not log {p_ticker} — see stdout for reason (lock timeout, or exposure cap reached).")
        except Exception as e:
            logger.error(f"Failed to analyze/log setup for {p_ticker}: {e}")

    logger.info("--- MADBOT ENGINE CYCLE FINISHED ---")

def main():
    # ── 5. NATIVE MUTEX (ATOMIC OS-LEVEL LOCK) ───────────────────────────────
    lock_file = os.path.join(script_dir, "engine.lock")
    
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode('utf-8'))
        os.close(fd)
    except FileExistsError:
        try:
            lock_age = time.time() - os.path.getmtime(lock_file)
            if lock_age > 2700:
                logger.warning(f"Found a dead lock file > 45 mins old ({lock_age/60:.1f}m). Forcing removal.")
                os.remove(lock_file)
                return 
            else:
                logger.warning(f"ABORT: Previous engine cycle is still running (Lock age: {lock_age/60:.1f}m). Exiting.")
                return 
        except OSError:
            return

    try:
        run_engine_cycle()
    except Exception as e:
        logger.critical(f"UNHANDLED ENGINE CRASH: {e}")
    finally:
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except OSError:
                pass

if __name__ == "__main__":
    main()