"""
screener.py v2.9.7 — MadBot Daily Screener
Fixes v2.9.7 (2026-07-24 — see analyze.py's matching changelog entry for the
full writeup; noted here because this file's ThreadPoolExecutor is what
triggers the race, even though the actual fix lives in analyze.py):
  ccxt's synchronous Exchange class isn't thread-safe for its own rate
       limiter (confirmed via a ccxt maintainer's direct response, not
       assumed — see analyze.py). This file's
       ThreadPoolExecutor(max_workers=cfg["max_workers"]) at line ~782 is
       what creates the concurrent analyze() calls that trigger it. Fixed in
       analyze.py's fetch_ohlcv() with per-exchange call locks — nothing to
       change here. Worth noting as a genuine positive side effect: those
       new locks, combined with this file's existing _throttle() dispatch
       pacer (both module-level, both correctly shared process-wide, not
       per-scan), substantially de-risk the "concurrent Streamlit users
       multiply Binance load" operational concern flagged in v2.9.6 below —
       _throttle() caps how fast NEW dispatches happen system-wide
       regardless of which user's scan triggered them, and the new locks cap
       actual concurrent Binance network calls at exactly 1 system-wide.
       Not a complete answer (max_workers is still set per scan call, so
       more concurrent users means more threads queued behind those two
       gates, not fewer), but meaningfully better protected than previously
       characterized.

Fixes v2.9.6 (2026-07-23 — Streamlit-readiness audit; each fix verified by
actually running the code, not just read and asserted correct):
  1. Import-order logging race — logging.basicConfig() configures the ROOT
       logger, and only the first call in a process has any effect; this
       file's call and analyze.py's own call were silently fighting over
       which one "won" depending on which file got imported first. Invisible
       as a CLI script; would silently suppress screener.py's own scan-
       progress log lines under a Streamlit app that imports both. Guarded
       behind `if __name__ == "__main__"` — CLI behavior unchanged (verified:
       identical root-logger level whether analyze or screener is imported
       first now), library-import behavior no longer touches global state.
  2. Import-time sys.exit(1) — a failed `from analyze import analyze` used to
       call sys.exit(1) at module scope, which raises SystemExit and
       terminates the whole interpreter. Fine for a CLI script; catastrophic
       once this file is imported into a long-lived multi-user process,
       where it would kill every connected session at once, not just fail
       the one triggering import. Replaced with a raised ImportError —
       verified with a real test (hid analyze.py, imported screener.py
       standalone): now raises a catchable ImportError instead of exiting.
  3. cfg passed/read by reference across the whole scan — screen() and every
       ThreadPoolExecutor worker it spawns read the same cfg dict for the
       life of a scan. A caller holding one shared cfg object across
       concurrent sessions (e.g. a Streamlit app with one config dict serving
       multiple users) and mutating it mid-scan could leak a second user's
       filter change into a first user's still-in-flight scan, silently
       mixing old and new threshold values across different tickers within
       one scan, no error anywhere. screen() now takes a defensive
       `cfg = dict(cfg)` shallow copy on entry, making its execution immune
       to any mutation of the original object after that point.
  NOT fixed here: the module-level CACHE_DIR.mkdir()/sys.stdout.reconfigure()
       calls at import. Checked this properly rather than continuing to
       guess (Streamlit Community Cloud docs, Hugging Face Spaces docs): the
       actual risk on common free-tier deploy targets is EPHEMERAL storage
       (the cache directory and any lock files vanish on redeploy/restart),
       not read-only mid-session — a milder, different characterization
       than originally stated here. mkdir(exist_ok=True) plus the existing
       fail-open try/except around every cache read/write already degrades
       gracefully either way (worst case: caching just doesn't help across
       restarts, still correct, not broken). No code change needed unless
       you land on a deploy target that's actually read-only mid-session,
       which neither of the common free options are.
  (The redundant per-horizon fetch pattern in annotate_outcomes.py, flagged
       here previously as unfixed, was fixed 2026-07-24 — see that file's
       own changelog for the full writeup and verification.)

Fixes v2.9.5 (2026-07-20 — 4 bugs found and verified by a parallel session,
independently re-confirmed against this exact code before fixing, per
project working rules):
  1. total_passed truncation — total_passed and the "Scan done" log line
       read from the top_N-truncated results list instead of the true
       pre-truncation pass count. This was a side effect of the 2026-07-14
       macro-bias fix (which correctly reordered apply_macro_filter before
       truncation, but didn't account for total_passed sharing that same
       truncated list downstream) — a regression from my own earlier fix,
       not a separate pre-existing issue. Fixed with a true_pass_count
       captured before truncation.
  2. Asymmetric bullish macro threshold — BULLISH was classified purely as
       "ratio <= 0.2" (low DOUBLE-BEARISH density), which is an absence-of-
       bearish check, not a bullish-confirmation check. A flat, 100%-neutral
       market could read BULLISH with zero actual bullish alignment. Fixed
       with a symmetric bullish_count/bullish_ratio check, same structure
       and threshold (0.7) as the existing bearish branch.
  3. Unguarded API-error-as-dict crash — a 200 OK from Binance's ticker/24hr
       carrying an error payload shaped as a dict (e.g. rate-limit message)
       passed raise_for_status() cleanly, then crashed the `for row in
       rows:` loop with an unguarded AttributeError instead of reaching the
       static_core fallback that already exists for exactly this situation.
       Fixed with an isinstance(rows, list) check inside the existing try.
  4. Ineffective throttle placement — inter_ticker_delay was a per-worker
       post-call sleep, which only ever spaced out one worker's own
       subsequent calls. It never throttled the initial concurrent burst
       where all max_workers threads hit Binance simultaneously at scan
       start. Replaced with a shared, lock-protected pacer (_throttle) that
       gates the DISPATCH moment before analyze() runs, across all workers.

Fixes v2.9.4 (2026-07-19 — robustness pass, paired with analyze.py fixes):
  force_refresh threading — scan_ticker() now accepts and passes force_refresh
       through to analyze(), sourced from screen()'s existing force_refresh
       param (--no-cache). Closes the gap where --no-cache busted this
       file's own scan cache but never reached analyze.py's OHLCV disk
       cache — see analyze.py changelog for the other half of this fix.
  Dead config cleanup — removed news_enabled/news_per_ticker/cryptopanic_token
       from DEFAULT_CONFIG. Confirmed zero other references in this file;
       analyze()'s signature has no parameter for any of them and the
       result dict hardcodes news: [] — not a forwarding bug, a fully
       removed feature with 3 stale keys left behind.

Fixes v2.9.3 (2026-07-18 — screener-side half of the 0-passed diagnosis):
  Error visibility (CONFIRMED gap, not a filter bug) — pretty_print_screener()
       never surfaced total_errors/scan_errors in the default table. screen()
       already computed both; a scan where every ticker crashed inside
       analyze() looked pixel-identical to a scan where 0 tickers genuinely
       cleared the filters — no way to tell the two apart from the table.
       Now prints a warning line with the error count when total_errors > 0.
  min_24h_volume_usd fallback mismatch (CONFIRMED, self-introduced in
       v2.9.2's cache-key fix) — the watchlist fetch call used a fallback of
       1_000_000 when the key was absent from config, but the cache key
       string built two lines later used a fallback of 0 for the same
       missing key. Harmless today (both are placeholders for "key absent"),
       but a latent collision: if min_24h_volume_usd is ever explicitly set
       to 0 in screener_config.json, its cache key would be identical to the
       key-absent case despite a genuinely different liquidity floor. Read
       once into min_24h_vol, same value used in both places now.
  NOTE: the suspected primary cause of the 0-passed scan (ccxt singletons in
       analyze.py never setting an explicit timeout, defaulting to ~10s
       against Indonesian-ISP latency to Binance) is NOT fixed here — that
       fix belongs in analyze.py, a separate file from what was asked to be
       patched this round. See chat for the open question on that.

Fixes v2.9.2 (2026-07-14 — gap analysis audit, all 4 verified before fixing,
2 corrected from broken proposed fixes rather than applied as given):

  Macro survivorship bias (CONFIRMED, most severe) — apply_macro_filter()
       used to run AFTER results was sliced to top_n (default 5), meaning
       macro bullish/bearish/neutral state — which controls a confidence
       downgrade applied to every displayed pick — was computed from only
       the top 5 highest-scoring survivors, not the full population that
       passed individual filters. Reproduced with a concrete test: the same
       25-ticker population that should read BULLISH read BEARISH under the
       old ordering, purely because 4 of the top 5 scorers happened to be
       bearish-aligned while the other 20 weren't. Fixed: macro is now
       computed on the full passing population BEFORE truncation; the
       truncated list is reattached to macro["picks"] afterward since that's
       what actually feeds the final display output (out["picks"],
       out["total_passed"]) — moving the call earlier without this second
       step would have silently leaked the full population into what's
       supposed to be a top-N display.
  Cache key omissions (CONFIRMED, self-introduced) — scan_cache_key never
       included min_24h_volume_usd or crypto_dynamic_top, both added in the
       v2.9 discovery rewrite. Changing either in config silently kept
       serving a stale cached scan for up to cache_ttl_minutes with no
       indication anything was wrong. Both now included in the key.
  Binance lock dead-end (diagnosis confirmed, proposed fix corrected) — on
       failure, _BINANCE_VALID = None meant every subsequent call during an
       outage re-attempted the full 20s blocking request. A proposed fix
       (set _BINANCE_VALID to a literal string sentinel) would have broken
       something worse: the string would pass the "is not None" fast-path
       check and get returned AS IF it were the valid-symbols set, turning
       every downstream `ticker in valid` into a substring check instead of
       set membership. Fixed properly with a timestamped 60s cooldown:
       verified immediately-after-failure returns None with no blocking
       call, and 61s-later correctly allows a real retry.
  Stablecoin false positives (diagnosis confirmed, proposed fix rejected) —
       the $0.98-$1.02 snapshot-price band has a permanent false-positive
       risk: any freely-floating token can cross that band from normal price
       action and get wrongly excluded, regardless of which coin it is
       today. A proposed fix (checking exchangeInfo "permissions" /
       "isMarginTradingAllowed") doesn't test for this at all — those fields
       describe which trading modes are enabled, not whether an asset is
       pegged. Fixed at zero extra API cost using priceChangePercent
       (already in the same ticker/24hr response): now requires price-near-$1
       AND minimal 24h movement together. Verified: a real peg (0.01% move)
       is excluded, ADA/XRP-style tokens merely passing through $1 with real
       24h movement (6.4%, -4.2%) are correctly kept.
  Stocks-in-scope-by-default (found during this audit, not in the original
       gap analysis) — CLI asset_type defaulted to "all", meaning running
       `screener.py` with no argument silently scanned IDX stocks alongside
       crypto, contradicting the crypto-only scope decided earlier in this
       project. Default changed to "crypto". Not removing the stock code —
       fetch_dynamic_stock_watchlist/IDX_UNIVERSE stay intact and reachable
       via explicit `stock` or `all` arguments — this is a reversible
       default change, not a deletion.

Fixes v2.9.1 (2026-07-14 — asset-class gap found in live output, MUB/SPCXB):
  Tokenized-equity exclusion — researched and confirmed MUB and SPCXB are
       Binance "bStocks": tokenized real-world equities (Micron Technology,
       SpaceX), launched 2026-06-11, backed 1:1 by real shares, tracking
       traditional stock prices via oracle feeds. USDT-quoted, real volume,
       not stablecoins — so none of the existing filters caught them, even
       though they directly contradict the crypto-only scope this project
       already committed to. All 6 confirmed bStocks tickers excluded.
       SAME DURABILITY LIMITATION AS THE STABLECOIN LIST: Binance stated
       more bStocks are coming "progressively," no fixed schedule — this
       list will go stale the same way the stablecoin list did with USD1.
       No verified structural signal was found to auto-detect these without
       live-testing Binance's actual exchangeInfo response for a known
       bStock symbol — documented in-code as a recommended follow-up rather
       than guessed at.

Fixes v2.9 (2026-07-09 — screener/analyze.py integration audit):
  Discovery — replaced CoinGecko trending/top-volume with Binance-native
       discovery (ticker/24hr, ranked by real 24h quote volume). Removes a
       third-party API dependency; measures liquidity on the exchange
       actually traded on, not aggregate cross-exchange attention.
  Stablecoin filter — extended the hardcoded exclusion list (was missing
       USD1, which passed the screener as a "BULLISH SQUEEZE" pick) AND
       added a price-proximity heuristic backstop (price within 2% of
       $1.00 is excluded regardless of whether the symbol is on the list)
       so future new stablecoins don't require remembering to update a list.
  Absolute liquidity floor — min_24h_volume_usd (new config key, default
       $1M) applied at DISCOVERY time, not as a post-analyze() filter — a
       ticker that can't clear the floor never costs an analyze() call.
       Previously only a relative-to-self volume ratio existed; researched
       against current screener/algo-trading community practice, which
       treats relative volume and an absolute floor as two distinct,
       complementary filter types, not substitutes for each other.
  Regime-aware ATR ceiling — max_atr_pct is now scaled per regime instead
       of one flat number for every regime (SQUEEZE tightened to 0.6x,
       TRENDING loosened to 1.2x, VOLATILE_AVOID tightened to 0.5x). NOTE:
       this one is engineering judgment applied to a confirmed gap
       (filtering was completely regime-blind despite analyze.py's scoring
       being regime-native since v7.2), not a documented external standard
       the way the volume floor above is — flagged as such, easy to revert.

Previous (v2.8):
  F4 — Binance timeout: 10s → 20s (Indonesian ISPs need more time reaching Binance).
       On timeout, _BINANCE_VALID is now set to None instead of the 21-ticker static list,
       so fetch_dynamic_crypto_watchlist skips the Binance filter rather than reducing
       the universe from 30+ (CoinGecko) to 21. Tickers that don't actually trade on
       Binance will fail at analyze() time and show up as scan errors instead.
  F5 — pretty_print_screener(): new formatted table output (human-readable by default).
       Use --json for raw JSON (e.g. piping to jq or agent use).
       Use --compact for agent-optimised JSON (unchanged from v2.7).
       Use --why to append rejected-ticker detail rows under the table.
  L7 — scan_cache_key now includes min_vol_ratio + max_atr_pct (was missing — stale cache on filter change)
  L8 — htf_1d_bias renamed to htf_bias (was wrong: 1H scan uses 4H HTF, not 1D)
       apply_macro_filter updated to use htf_bias consistently
       * Suppresses INFO log lines to stderr (pty captures stderr → bloats tool result context)
       * Uses json.dumps(separators=(',',':')) instead of indent=2 (~20% char reduction)
       * Strips agent-irrelevant metadata fields: _from_cache, scan_errors, rejected_detail
       * ZERO changes to signal logic, filters, or picks structure
       * Use this flag in ALL OpenClaw screener exec calls to reduce token usage
Previous (v2.5):
  B2 — _BINANCE_VALID global protected with threading.Lock (double-checked locking)
       Prevents duplicate Binance exchangeInfo calls when 6+ threads initialize simultaneously
  B4 — apply_macro_filter now works for stocks:
       Stocks use htf_1d_bias only (btc_1h_bias is always "N/A" for stocks → old code was a no-op)
Previous (v2.4):
  N3 — parse_idr_price() removed (dead code since D1 fix)
  N4 — apply_macro_filter minimum sample guard added (need ≥ 3 picks for BEARISH state)
  N5 — apply_macro_filter confidence downgrade fixed (chained replace → lookup dict)
  N6 — scan_ticker collects ALL filter failure reasons (elif chain → independent if checks)
Previous (v2.3):
  module-level analyze import, htf_bias/btc_bias dict level,
  atr_pct currency fix, --top flag, IDX universe expanded to LQ45
"""
import sys, json, time, argparse, logging, os, hashlib, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests

# ── UTF-8 stdout fix (Windows cp1252 → UnicodeEncodeError on emoji/Unicode) ──
import io as _io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# ─────────────────────────────────────────────────────────────────────────────

# BUG FIX (audit finding, 2026-07-23): logging.basicConfig() configures the
# ROOT logger, and only the first call in a process wins — every later call
# elsewhere (e.g. analyze.py's own basicConfig() call) is a silent no-op. As
# a CLI script that's invisible; the moment this file and analyze.py are both
# imported into one long-lived process (a Streamlit app importing both),
# whichever gets imported first wins and the other file's intended log level
# is silently discarded. Guarding this behind __main__ keeps CLI usage
# (python screener.py ...) byte-for-byte unchanged, while importing this
# module as a library no longer touches global state. NOTE for whoever
# writes the Streamlit entrypoint: when screener.py is only imported (not
# run as __main__), the "screener" logger below has no handler attached, so
# log.info(...) scan-progress lines go nowhere by default — configure
# logging explicitly in the app entrypoint, once, before importing this
# module, if you want to see them.
if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("screener")

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "screener_config.json"
CACHE_DIR   = BASE_DIR / ".screener_cache"
CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "min_score": 2, "min_vol_ratio": 0.8, "max_atr_pct": 10.0, "timeframe": "1H",
    "top_picks": 5, "crypto_dynamic_top": 20, "stock_universe_size": 30,
    "max_workers": 4, "cache_ttl_minutes": 30, "inter_ticker_delay": 0.3,
    # CLEANUP (2026-07-19): news_enabled/news_per_ticker/cryptopanic_token
    # removed — analyze() has no parameter for any of them (signature is
    # ticker, timeframe, asset_type, skip_mtf, force_refresh) and hardcodes
    # "news": [] in its result dict. Not a forwarding bug, a fully removed
    # feature (looks like it went away in the v7.0 rewrite) with 3 stale
    # keys left behind in config. If you still want news fetching, it needs
    # to be rebuilt in analyze.py, not just re-added here.
}


# BUG FIX (audit finding, 2026-07-23): this used to call sys.exit(1) here,
# which raises SystemExit and terminates the whole interpreter. That's fine
# for a CLI script (screener.py won't run without analyze.py, so exiting is
# correct) but catastrophic once this file is imported into a long-lived,
# multi-user process — a broken import would kill every connected session at
# once, not just fail gracefully for the one request that triggered it.
# Raising a normal ImportError lets the caller (CLI __main__ block below, or
# a Streamlit app entrypoint) decide how to handle it — the CLI path still
# ends the process (via the normal uncaught-exception traceback), it just no
# longer does so in a way that bypasses a host application's own error
# handling if imported as a library.
try:
    from analyze import analyze
except ImportError as e:
    raise ImportError(
        f"screener.py requires analyze.py to be importable: {e}. "
        f"Ensure both files are in the same directory as your entrypoint "
        f"(or on PYTHONPATH)."
    ) from e


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
            log.info(f"Config loaded from {CONFIG_PATH}")
        except Exception as e:
            log.warning(f"Could not read config file ({e}), using defaults")
    else:
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            log.info(f"Created default config at {CONFIG_PATH}")
        except Exception:
            pass
    return cfg


def cache_key(name: str) -> Path:
    safe = hashlib.md5(name.encode()).hexdigest()[:12]
    return CACHE_DIR / f"{name.replace('/', '')}_{safe}.json"


def cache_get(name: str, ttl_minutes: int):
    if ttl_minutes <= 0:
        return None
    path = cache_key(name)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            entry = json.load(f)
        if (time.time() - entry["ts"]) / 60 < ttl_minutes:
            log.debug(f"Cache HIT: {name}")
            return entry["data"]
    except Exception:
        pass
    return None


def cache_set(name: str, data):
    path = cache_key(name)
    try:
        with open(path, "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except Exception as e:
        log.warning(f"Cache write failed for {name}: {e}")


# FIX B2: double-checked locking — prevents multiple Binance exchangeInfo calls
# when ThreadPoolExecutor launches 6 workers before first thread finishes init.
_BINANCE_VALID: set = None
_BINANCE_LOCK: threading.Lock = threading.Lock()
_BINANCE_LAST_FAILURE: float = 0.0  # timestamp of last exchangeInfo failure, for cooldown

# BUG FIX (2026-07-20, confirmed via cross-session verification):
# inter_ticker_delay used to be a time.sleep() inside scan_ticker's finally
# block — AFTER that call's work completed. Since all max_workers threads
# get their first ticker from the queue and start it immediately, that sleep
# only ever spaced out a single worker's OWN subsequent calls; it never
# throttled the initial concurrent burst where every worker hits Binance at
# once. A real fix needs a pacer shared across all workers, gating the
# DISPATCH moment (before analyze() runs), not a per-worker post-call sleep.
_THROTTLE_LOCK = threading.Lock()
_THROTTLE_LAST = [0.0]

def _throttle(delay: float):
    """Global pacer: blocks the calling thread until at least `delay`
    seconds have passed since ANY worker's last dispatch, not just this
    worker's own. Called before analyze(), not after — this is what
    actually spaces out ticker-to-ticker Binance hits, including the first
    call each worker makes."""
    with _THROTTLE_LOCK:
        wait = _THROTTLE_LAST[0] + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        _THROTTLE_LAST[0] = time.time()

def fetch_binance_valid_symbols(ttl: int = 240) -> set:
    global _BINANCE_VALID, _BINANCE_LAST_FAILURE
    # Fast path: already initialized (no lock needed for read after init)
    if _BINANCE_VALID is not None:
        return _BINANCE_VALID
    # BUG FIX (audit finding, 2026-07-14 — confirmed real, Gemini's proposed
    # fix was broken): on failure this used to set _BINANCE_VALID = None,
    # which meant EVERY subsequent call — across every thread, every ticker,
    # for as long as this process runs — would fail this fast-path check and
    # fall through to the 20-second blocking request again, even seconds
    # after a confirmed outage. Gemini's proposed fix set _BINANCE_VALID to a
    # literal string ("ERROR_STATE_SENTINEL") to make it "not None" — but
    # that string would then pass this exact check and get returned AS IF it
    # were the valid-symbols set, and every downstream `if ticker in valid`
    # check would do STRING SUBSTRING matching instead of set membership,
    # silently corrupting the filter rather than fixing the hang. Fixed
    # properly: track when the last failure happened, and only re-attempt
    # after a real cooldown window has passed — otherwise fail fast and
    # return None immediately, exactly like a live outage should behave.
    COOLDOWN_SECONDS = 60
    if _BINANCE_LAST_FAILURE and (time.time() - _BINANCE_LAST_FAILURE) < COOLDOWN_SECONDS:
        return None
    # Slow path: acquire lock, then double-check (another thread may have initialized while we waited)
    with _BINANCE_LOCK:
        if _BINANCE_VALID is not None:
            return _BINANCE_VALID
        if _BINANCE_LAST_FAILURE and (time.time() - _BINANCE_LAST_FAILURE) < COOLDOWN_SECONDS:
            return None
        cached = cache_get("binance_valid_symbols", ttl)
        if cached:
            _BINANCE_VALID = set(cached)
            return _BINANCE_VALID
        try:
            r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=20)
            r.raise_for_status()
            symbols = {
                s["baseAsset"].upper() for s in r.json().get("symbols", [])
                if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
            }
            _BINANCE_VALID = symbols
            cache_set("binance_valid_symbols", list(symbols))
            return _BINANCE_VALID
        except Exception as e:
            log.warning(f"Binance exchangeInfo failed: {e} — cooling down {COOLDOWN_SECONDS}s before retry")
            # Still None so callers can detect "Binance unavailable" and skip
            # the filter (avoids reducing the universe down to a tiny static
            # fallback), but _BINANCE_LAST_FAILURE now prevents every single
            # subsequent call from re-attempting the blocking request during
            # an active outage.
            _BINANCE_VALID = None
            _BINANCE_LAST_FAILURE = time.time()
            return _BINANCE_VALID


def fetch_dynamic_crypto_watchlist(top_n: int = 20, ttl: int = 30, min_usd_vol: float = 1_000_000) -> list:
    """
    v2.9: Binance-native discovery, replacing CoinGecko trending/top-volume.

    WHY: you trade exclusively on Binance/Indodax. CoinGecko's ranking measures
    aggregate cross-exchange attention/volume, which isn't guaranteed to be
    liquid on Binance specifically. Ranking directly by Binance's own 24h
    quote volume measures exactly what matters for you: can this actually be
    traded, in size, on the exchange you use. It also removes a third external
    API dependency entirely (one less thing that rate-limits, times out, or
    changes its API on you) and reuses the exact endpoint style already used
    by fetch_binance_valid_symbols() below, for consistency.

    Researched against current community/industry screener practice before
    building this (2026-07): relative-volume ranking (this) and an absolute
    USD volume floor (min_usd_vol) are both named as standard, separate
    filter types — not something to pick one over the other. Added the floor
    here, at DISCOVERY time, not as a post-analyze() filter: a ticker that
    can't clear the liquidity floor should never cost an analyze() API call
    in the first place. That research also flagged that trusting any single
    exchange's reported volume for smaller/newer pairs carries real wash-
    trading risk — the floor is partly a defense against exactly that, not
    just a liquidity check.
    """
    cache_name = f"crypto_watchlist_{top_n}_{min_usd_vol}"
    cached = cache_get(cache_name, ttl)
    if cached:
        return cached

    # Audit finding (2026-07-09): this list is inherently a moving target —
    # USD1 was missing and slipped through as a "BULLISH SQUEEZE" pick despite
    # being a stablecoin. Extended with other known stablecoins, but see the
    # price-proximity heuristic below for a backstop that doesn't rely on
    # this list staying current.
    # Audit finding (2026-07-14): MUB and SPCXB passed the screener as crypto
    # picks. Researched and confirmed: these are Binance "bStocks" — tokenized
    # real-world equities (MUB = Micron Technology, SPCXB = SpaceX), launched
    # 2026-06-11, backed 1:1 by actual shares in custody, tracking traditional
    # equity prices via oracle feeds (and explicitly noted to behave
    # differently on weekends — "secondary private market valuations" — when
    # underlying stock markets are closed). They trade as USDT pairs, which is
    # exactly why the crypto-quote/liquidity/stablecoin filters didn't catch
    # them — they aren't stablecoins, they clear real volume, they just aren't
    # crypto. This directly contradicts the crypto-only scope already decided
    # earlier in this project. All 6 confirmed bStocks tickers as of the
    # research date below are excluded. SAME DURABILITY CAVEAT AS THE
    # STABLECOIN LIST: Binance's own announcement states additional bStocks
    # will be added "progressively" with no fixed schedule — this list WILL
    # go stale the same way the stablecoin list did with USD1. No verified
    # structural signal (e.g. a distinguishing exchangeInfo field) was found
    # to auto-detect these without live-testing Binance's actual API response
    # for a known bStock symbol — recommend checking that directly (e.g.
    # `requests.get(".../exchangeInfo").json()` filtered to "MUBUSDT", read
    # every field, compare against an ordinary pair like "BTCUSDT") to find a
    # real distinguishing field before trusting a pattern-based rule here.
    tokenized_equities = {"MUB", "SPCXB", "CRCLB", "NVDAB", "TSLAB", "SNDKB"}  # bStocks, confirmed 2026-07-14

    stablecoins = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDS",
                   "PYUSD", "FRAX", "GUSD", "USD1", "USDP", "EURC", "USDD", "LUSD"}

    static_core = [
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        "ATOM", "LTC", "TRX", "SUI", "TON", "PEPE", "SHIB", "NEAR", "UNI", "OP", "ARB",
    ]

    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=20)
        r.raise_for_status()
        rows = r.json()
        # BUG FIX (2026-07-20, confirmed via cross-session verification): a
        # 200 OK with an error payload shaped as a dict (e.g. Binance's
        # {"code":-1003,"msg":"..."}) passed raise_for_status() cleanly
        # (status code is fine, it's the body that's wrong), then hit the
        # `for row in rows:` loop below — iterating a dict yields its string
        # KEYS, and `row.get("symbol")` on a string raised an unguarded
        # AttributeError, crashing discovery entirely instead of reaching
        # the static_core fallback this except block already exists for.
        if not isinstance(rows, list):
            raise ValueError(f"expected a list from ticker/24hr, got {type(rows).__name__}: {rows}")
    except Exception as e:
        log.warning(f"Binance 24hr ticker fetch failed ({e}) — falling back to static core list only")
        result = sorted(set(static_core))
        cache_set(cache_name, result)
        return result

    candidates = []
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4].upper()
        # BUG FIX (audit finding, 2026-09): a live scan log showed a
        # malformed non-ASCII "ticker" (rendered as Chinese characters)
        # reaching analyze() and erroring on both Binance and Indodax — its
        # origin couldn't be fully traced (Binance's real ticker/24hr
        # symbols are always plain ASCII, so this most likely means
        # something downstream mangled a symbol during parsing, though that
        # is inference, not confirmed root cause). Whatever the source, a
        # single bad symbol reaching fetch_ohlcv() can trip the shared
        # "binance" circuit breaker for the full cooldown window, fail-fast
        # rejecting every OTHER ticker in that scan for up to 60s (see the
        # matching analyze.py _resilient_call fix). Cheap, defensive
        # sanity check: a real Binance base asset is plain ASCII
        # alphanumerics only. Reject anything else here, before it ever
        # becomes a candidate.
        if not base.isascii() or not base.isalnum():
            continue
        if base in stablecoins:
            continue
        if base in tokenized_equities:
            continue
        try:
            quote_vol   = float(row.get("quoteVolume", 0) or 0)
            last_price  = float(row.get("lastPrice", 0) or 0)
            price_chg_pct = float(row.get("priceChangePercent", 0) or 0)
        except (TypeError, ValueError):
            continue
        # BUG FIX (audit finding, 2026-07-14 — diagnosis confirmed real,
        # Gemini's proposed fix rejected): a snapshot-price-near-$1 check has
        # a genuine, permanent false-positive risk — ANY freely-floating
        # token can pass through the $0.98-$1.02 band purely from normal
        # price action at some point, and would get wrongly excluded every
        # time that happens, regardless of which specific coin it is today.
        # Gemini's proposed fix (checking "permissions"/"isMarginTradingAllowed"
        # from exchangeInfo) does not actually test for this — those fields
        # describe which TRADING MODES Binance enables for a pair, not
        # whether the underlying asset is pegged to $1; a legitimate volatile
        # altcoin can have limited margin permissions for unrelated liquidity
        # reasons. Fixed properly, at zero extra API cost: a real stablecoin
        # isn't just near $1 right now, it STAYS near $1 — priceChangePercent
        # is already in this same ticker/24hr response. Require both
        # conditions together: price near $1 AND minimal 24h movement. A
        # legitimate token merely passing through $1 will show real
        # 24h movement and correctly survive; an actual peg won't.
        if 0.98 <= last_price <= 1.02 and abs(price_chg_pct) < 1.0:
            continue
        if quote_vol < min_usd_vol:
            continue
        candidates.append((base, quote_vol))

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_by_volume = [c[0] for c in candidates[:top_n]]

    tickers = set(top_by_volume)
    tickers.update(static_core)

    # BUG FIX (audit finding, 2026-09): defense in depth, complementing the
    # extraction-time filter above. static_core is a hardcoded, trusted
    # list, but the fetch_binance_valid_symbols() check below is SKIPPED
    # ENTIRELY when Binance is unavailable ("skipping pair filter, using
    # full watchlist" a few lines down) — precisely the scenario (Binance
    # having connectivity trouble) most likely to accompany a malformed
    # ticker slipping through unfiltered. This runs unconditionally, before
    # that check, so a bad symbol can't survive purely because Binance
    # happened to be down at the same time.
    tickers = {t for t in tickers if t.isascii() and t.isalnum()}

    valid = fetch_binance_valid_symbols(ttl)
    if valid:
        before = len(tickers)
        tickers = {t for t in tickers if t in valid}
        dropped = before - len(tickers)
        if dropped:
            log.info(f"Dropped {dropped} non-Binance USDT pairs")
    else:
        log.warning("Binance unavailable — skipping pair filter, using full watchlist")
    result = sorted(tickers)
    cache_set(cache_name, result)
    return result


IDX_UNIVERSE = [
    # LQ45 core blue chips
    "BBCA", "BBRI", "BMRI", "TLKM", "ASII", "UNVR", "GOTO", "ADRO",
    "TOWR", "BSDE", "CTRA", "SMRA",
    # LQ45 additions (financials)
    "BBNI", "BRIS", "ARTO", "BTPS", "PNBN",
    # LQ45 additions (energy/commodity)
    "PGAS", "PTBA", "INCO", "MDKA", "BUMI",
    # LQ45 additions (consumer/industrial)
    "INDF", "ICBP", "MYOR", "KLBF", "SIDO", "CPIN",
    "AALI", "LSIP", "JPFA",
    # LQ45 additions (tech/telecom)
    "EXCL", "ISAT", "MTEL",
    # LQ45 additions (property/construction)
    "PWON", "BKSL", "JSMR", "WSKT",
    # Active mid-caps often in IDX80
    "INTP", "SMGR", "ANTM", "PTPP",
]

def fetch_dynamic_stock_watchlist(ttl: int = 30) -> list:
    # IDX_UNIVERSE is a static list — cache provides interface parity with crypto watchlist
    # and allows future dynamic expansion (e.g. pulling from IDX website) without API changes.
    cached = cache_get("stock_watchlist", ttl)
    if cached:
        return cached
    result = list(IDX_UNIVERSE)
    cache_set("stock_watchlist", result)
    return result


def scan_ticker(ticker: str, asset_type: str, cfg: dict, diagnostic: bool = False,
                 force_refresh: bool = False) -> dict | None:
    # BUG FIX (2026-07-20): throttle now gates dispatch (see _throttle above),
    # not a post-call sleep — actually spaces out the initial concurrent
    # burst across all workers instead of only a single worker's own re-use.
    _throttle(cfg.get("inter_ticker_delay", 0.3))
    try:
        # BUG FIX (audit finding, 2026-07-19): analyze() had no way to be told
        # to bypass its own internal OHLCV disk cache (independent TTLs —
        # 600s/1H, 1800s/4H, 14400s/1D). --no-cache only ever busted
        # screener.py's own scan-level cache; analyze()'s candle data could
        # still be silently up to those TTLs old on a "fresh" --no-cache run,
        # with no indication in the output that it happened.
        res = analyze(ticker, timeframe=cfg["timeframe"], asset_type=asset_type,
                       force_refresh=force_refresh)
    except Exception as e:
        return {"_error": f"analyze() crashed: {e}", "ticker": ticker}

    if not isinstance(res, dict) or "error" in res:
        return {"_error": res.get("error", "unknown") if isinstance(res, dict) else "non-dict", "ticker": ticker}

    signal_block  = res.get("signal", {}) or {}
    score         = signal_block.get("score", 0)
    no_trade      = signal_block.get("no_trade", False)
    warnings_list = signal_block.get("warnings", [])

    vol_block     = res.get("volume") or {}
    vol_ratio     = vol_block.get("ratio")
    effective_vol = vol_ratio if (vol_ratio is not None) else 0.0

    atr_pct = res.get("atr_pct")
    if atr_pct is None:
        atr_pct = 99.0  # safe fallback — triggers max_atr filter if analyze.py is outdated

    regime = res.get("regime", "N/A")

    min_score = cfg.get("min_score", 3)
    min_vol   = cfg.get("min_vol_ratio", 1.0)
    max_atr   = cfg.get("max_atr_pct", 8.0)

    # v2.9: regime-aware ATR tolerance. NOTE (audit, 2026-07-09): this is my
    # own engineering judgment applied to a real, confirmed gap (this filter
    # used to be completely regime-blind despite analyze.py's scoring being
    # regime-native since v7.2) -- it is NOT a documented external industry
    # standard the way the volume-ratio/absolute-floor combination above is.
    # Reasoning: SQUEEZE is low-volatility by definition, so the same flat
    # ceiling that's reasonable for a TRENDING ticker is looser than it
    # needs to be there; a TRENDING ticker can carry more volatility and
    # still be a valid setup. Conservative multipliers, easy to tune or
    # revert to flat 1.0 across the board if this doesn't hold up in practice.
    REGIME_ATR_MULTIPLIER = {
        "SQUEEZE":         0.6,
        "TRENDING_UP":     1.2,
        "TRENDING_DOWN":   1.2,
        "RANGING":         1.0,
        "VOLATILE_AVOID":  0.5,
    }
    effective_max_atr = max_atr * REGIME_ATR_MULTIPLIER.get(regime, 1.0)

    # FIX N6: collect ALL filter reasons (was elif chain — only first reason was reported)
    reasons = []
    if score < min_score:
        reasons.append(f"score {score} < min {min_score}")
    if no_trade:
        reasons.append("no_trade")
    if effective_vol < min_vol:
        reasons.append(f"vol {effective_vol:.2f} < min {min_vol}")
    if atr_pct > effective_max_atr:
        reasons.append(f"atr {atr_pct:.2f}% > regime-adjusted max {effective_max_atr:.2f}% ({regime})")

    if reasons:
        why = "; ".join(reasons)
        if not diagnostic:
            return None
        return {
            "_diagnostic": True, "_why_rejected": why, "ticker": ticker,
            "score": score, "volume_vs_avg": round(effective_vol, 3),
            "atr_pct": round(atr_pct, 2), "no_trade": no_trade,
            "strategy": signal_block.get("strategy", "N/A"),
            "top_signals": signal_block.get("signals", [])[:3], "warnings": warnings_list,
        }

    confidence = signal_block.get("confidence", "N/A")
    htf_bias   = signal_block.get("htf_bias", "N/A")
    btc_bias   = signal_block.get("btc_bias", "N/A")
    strategy   = signal_block.get("strategy", "N/A")
    trade      = res.get("trade_setup", {})
    entry_zone = res.get("entry_zone", {})
    news       = res.get("news", [])

    return {
        "ticker": ticker, "asset_type": asset_type, "score": score,
        "confidence": confidence, "regime": regime,
        "htf_bias": htf_bias, "btc_1h_bias": btc_bias, "strategy": strategy,
        "price":        res.get("price_idr", "N/A"),
        "volume_vs_avg": round(effective_vol, 3),
        "atr_pct":      round(atr_pct, 2),
        "sl_price":     trade.get("sl", "N/A"),
        "tp2_price":    trade.get("tp2", "N/A"),
        "sl_pct":       trade.get("sl_pct", "N/A"),
        "tp2_pct":      trade.get("tp2_pct", "N/A"),
        "pct_pool":     trade.get("pct_pool", "N/A"),
        "risk_idr":     trade.get("risk_idr", "N/A"),
        "top_signals":  signal_block.get("signals", [])[:3],
        "warnings":     warnings_list,
        "_raw_score":   score,
    }


# ── MACRO CORRELATION FILTER ──────────────────────────────────────────────────
# Confidence grade order for downgrade lookup
_CONF_DOWNGRADE = {"High": "Moderate", "Moderate": "Low", "Low": "Low", "Very low": "Very low"}

def apply_macro_filter(picks: list[dict]) -> dict:
    """
    FIX N4: macro state requires ≥ 3 picks to avoid false BEARISH on tiny samples.
    FIX N5: confidence downgrade uses lookup dict (was chained .replace() that always ran both steps).
    FIX B4: stocks use htf_1d_bias only — btc_1h_bias is always "N/A" for stocks,
            making the old (btc AND htf) condition a permanent no-op for stock scans.
    """
    # N4: too few picks → can't determine meaningful macro state
    if len(picks) < 3:
        return {"macro_context": "NEUTRAL", "picks": picks, "warning": None}

    # B4: detect asset_type from first pick (screen() always sends one asset_type at a time)
    asset_type = picks[0].get("asset_type", "crypto") if picks else "crypto"

    if asset_type == "stock":
        # Stocks have no BTC context — use HTF bias alone as macro signal
        bearish_count = sum(1 for p in picks if p.get("htf_bias") == "bearish")
        bullish_count = sum(1 for p in picks if p.get("htf_bias") == "bullish")
    else:
        # Crypto: require BOTH BTC drag AND HTF bearish to call macro bearish
        bearish_count = sum(
            1 for p in picks
            if p.get("btc_1h_bias") == "bearish" and p.get("htf_bias") == "bearish"
        )
        # BUG FIX (2026-07-20, confirmed via cross-session verification):
        # BULLISH was previously classified purely as "ratio <= 0.2" — i.e.
        # low DOUBLE-BEARISH density — which is an absence-of-bearish check,
        # not a bullish-confirmation check. A flat, 100% neutral market (0%
        # bearish AND 0% bullish alignment) produced ratio=0.0 and was
        # labeled BULLISH with zero actual bullish evidence behind it. Fixed
        # by tracking bullish_count with the same rigor as bearish_count —
        # requires BOTH BTC drag AND HTF bullish, same structure, same
        # threshold (0.7) as the bearish branch below.
        bullish_count = sum(
            1 for p in picks
            if p.get("btc_1h_bias") == "bullish" and p.get("htf_bias") == "bullish"
        )

    ratio = bearish_count / len(picks)
    bullish_ratio = bullish_count / len(picks)
    macro_state = "NEUTRAL"
    if ratio >= 0.7:
        macro_state = "BEARISH"
        for p in picks:
            # Stocks: downgrade if HTF bearish; crypto: downgrade if BTC bearish
            should_downgrade = (
                p.get("htf_bias") == "bearish" if asset_type == "stock"
                else p.get("btc_1h_bias") == "bearish"
            )
            if should_downgrade:
                conf = p.get("confidence", "")
                # N5: break-on-first-match — only one downgrade step applied
                for grade, downgraded in _CONF_DOWNGRADE.items():
                    if grade in conf:
                        p["confidence"] = conf.replace(grade, downgraded, 1)
                        break
    elif bullish_ratio >= 0.7:
        macro_state = "BULLISH"
    return {
        "macro_context": macro_state,
        "picks": picks,
        "warning": "⚠️ Macro bearish. Position sizes auto-reduced to 10% pool." if macro_state == "BEARISH" else None,
    }


def screen(asset_type: str, cfg: dict, force_refresh: bool = False, diagnostic: bool = False) -> dict:
    # BUG FIX (audit finding, 2026-07-23): defensive shallow copy. cfg is read
    # by every ThreadPoolExecutor worker for the life of this scan. If a
    # caller holds a reference to the same dict across concurrent callers
    # (e.g. a Streamlit app serving multiple sessions from one shared config
    # object) and mutates it while a scan from a different session is still
    # in flight, that mutation would otherwise leak in — silently mixing old
    # and new filter values across different tickers within one scan, with no
    # error. Copying once here makes this function's execution immune to any
    # mutation of the original object after this point. Cheap: cfg is flat
    # (str/float/int values only), so a shallow copy is sufficient.
    cfg = dict(cfg)
    ttl = 0 if force_refresh else cfg.get("cache_ttl_minutes", 30)
    # BUG FIX (audit finding, 2026-07-18): min_24h_volume_usd was read with two
    # DIFFERENT fallback defaults — 1_000_000 here for the actual watchlist
    # fetch, but 0 in the cache key below. Harmless while the key is simply
    # absent from config (both cases were placeholders), but a latent
    # collision risk: if min_24h_volume_usd is ever explicitly set to 0 in
    # screener_config.json, the cache key would be identical to the
    # key-absent case even though the real liquidity floor differs (0 vs
    # 1,000,000). Read once, use the same value + same fallback everywhere.
    min_24h_vol = cfg.get("min_24h_volume_usd", 1_000_000)
    watchlist = (
        fetch_dynamic_crypto_watchlist(cfg.get("crypto_dynamic_top", 20), ttl, min_24h_vol)
        if asset_type == "crypto"
        else fetch_dynamic_stock_watchlist(ttl)
    )
    # BUG FIX (audit finding, 2026-07-14): scan_cache_key omitted
    # min_24h_volume_usd and crypto_dynamic_top — both added in the v2.9
    # discovery rewrite, and both never wired into the cache key at the time.
    # Without this, changing either in screener_config.json (e.g. raising the
    # liquidity floor to fight wash trading, or widening the scan range)
    # produces the exact same cache key as before the change, silently
    # serving a stale cached scan for up to cache_ttl_minutes with no
    # indication anything is wrong.
    scan_cache_key = (
        f"scan_{asset_type}_{cfg['timeframe']}_{cfg['min_score']}_"
        f"{cfg['min_vol_ratio']}_{cfg['max_atr_pct']}_"
        f"{min_24h_vol}_{cfg.get('crypto_dynamic_top', 0)}"
    )
    if not force_refresh and not diagnostic:
        cached = cache_get(scan_cache_key, ttl)
        if cached:
            cached["_from_cache"] = True
            return cached

    log.info(f"Scanning {len(watchlist)} {asset_type} tickers with {cfg['max_workers']} threads...")
    results, errors, rejected = [], [], []
    with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
        futures = {pool.submit(scan_ticker, t, asset_type, cfg, diagnostic, force_refresh): t for t in watchlist}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                r = future.result(timeout=30)
                if r is None:
                    pass
                elif r.get("_diagnostic"):
                    rejected.append(r)
                    log.info(f"  ✗ {ticker:8s} → {r['_why_rejected']}")
                elif "_error" in r:
                    errors.append({"ticker": ticker, "error": r["_error"]})
                    log.warning(f"  ✗ {ticker:8s} ERROR → {r['_error']}")
                else:
                    results.append(r)
                    log.info(f"  ✓ {ticker:8s} PASSED score={r['score']}")
            except Exception as e:
                errors.append({"ticker": ticker, "error": str(e)})
                log.warning(f"  ✗ {ticker:8s} CRASH → {e}")

    # BUG FIX (audit finding, 2026-07-14 — confirmed via gap analysis, verified
    # against this exact code before fixing): apply_macro_filter() used to run
    # AFTER results was already sliced to top_n (default 5). That meant macro
    # context — bullish/bearish/neutral, which controls a confidence downgrade
    # applied to every displayed pick — was being computed from only the top 5
    # highest-scoring survivors, not the full population of tickers that
    # actually passed the individual score/vol/atr filters. Concretely: with
    # top_picks=5, just 4 of 5 top picks showing bearish alignment was enough
    # to declare the ENTIRE scan bearish and cut every position size to 10% —
    # regardless of how the other passing tickers actually looked. The macro
    # filter's own "need >=3 picks" sample-size guard was clearly written
    # assuming it protects against a genuinely small scan result, not a result
    # deliberately truncated to top_n one line earlier. Fixed: macro context is
    # now computed across every ticker that passed the filters, THEN the list
    # is truncated for display — the truncation no longer feeds back into the
    # macro assessment at all.
    macro = apply_macro_filter(results)

    # BUG FIX (2026-07-20, confirmed via cross-session verification): the line
    # below that reattaches the top-N-truncated list into macro["picks"] for
    # display was, until now, also the ONLY source total_passed read from —
    # meaning total_passed silently reported min(true_pass_count, top_n)
    # instead of the true number of tickers that cleared score/vol/atr. This
    # was a side effect of the 2026-07-14 macro-bias fix (which correctly
    # moved apply_macro_filter before truncation, but didn't account for
    # total_passed sharing that same truncated list downstream). Capturing
    # the true count here, before truncation, fixes it without touching the
    # truncation or macro logic above.
    true_pass_count = len(results)

    top_n = cfg.get("top_picks", 5)
    results = sorted(results, key=lambda x: x["_raw_score"], reverse=True)[:top_n]
    for i, r in enumerate(results):
        r["rank"] = i + 1
        r.pop("_raw_score", None)
    # macro["picks"] currently holds the FULL pre-slice population (needed
    # above for an unbiased macro assessment) but macro["picks"] is also what
    # populates the final display output below (out["picks"], out["total_passed"])
    # — reattach the top-N-sliced list here so display still shows only top_n,
    # not every ticker that merely passed the individual filters.
    macro["picks"] = results

    log.info(f"Scan done: {len(watchlist)} scanned, {true_pass_count} passed, {len(errors)} errors")
    if errors:
        log.warning(f"  ⚠️ {len(errors)} tickers errored — check scan_errors in output")

    out = {
        "asset_type":         asset_type,
        "timeframe":          cfg["timeframe"],
        "scan_time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "macro_context":      macro["macro_context"],
        "macro_warning":      macro.get("warning"),
        "filters_used":       {"min_score": cfg["min_score"], "min_vol_ratio": cfg["min_vol_ratio"], "max_atr_pct": cfg["max_atr_pct"]},
        "total_in_watchlist": len(watchlist),
        "total_passed":       true_pass_count,
        "total_errors":       len(errors),
        "picks":           macro["picks"],
        "scan_errors":     len(errors),
        "rejected_detail": rejected if diagnostic else [],
        "_from_cache":     False,
    }
    cache_set(scan_cache_key, out)
    return out


def pretty_print_screener(data: dict) -> None:
    """
    Render screener results as a human-readable terminal table.
    Called by default from __main__; use --json for the raw JSON output.
    """
    import re

    def _usd(price_str: str) -> str:
        """Extract '$X.XX' from 'Rp X ($Y)' — falls back to IDR snippet."""
        if not price_str or price_str in ("N/A", "—"):
            return "—"
        m = re.search(r"\((\$[^)]+)\)", price_str)
        if m:
            return m.group(1)
        m = re.search(r"Rp ([0-9,.]+)", price_str)
        return f"Rp {m.group(1)}" if m else price_str

    picks      = data.get("picks", [])
    rejected   = data.get("rejected_detail", [])
    asset      = data.get("asset_type", "").upper()
    tf         = data.get("timeframe", "?")
    scan_time  = data.get("scan_time", "?")
    macro      = data.get("macro_context", "NEUTRAL")
    macro_warn = data.get("macro_warning")
    filters    = data.get("filters_used", {})
    total_in   = data.get("total_in_watchlist", "?")
    total_pass = data.get("total_passed", len(picks))
    total_err  = data.get("total_errors", 0)
    cached     = data.get("_from_cache", False)

    W   = 74
    SEP = "═" * W
    DIV = "─" * W
    macro_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(macro, "⚪")

    lines = []
    a = lines.append

    a(f"\n{SEP}")
    cache_tag = "  [CACHED]" if cached else ""
    a(f"  {asset} SCREENER  │  {tf}  │  {scan_time}{cache_tag}")
    a(f"  {macro_icon} MACRO: {macro}  │  {total_in} scanned  │  {total_pass} passed")
    a(f"  Filters: score≥{filters.get('min_score',2)}  "
      f"vol≥{filters.get('min_vol_ratio',0.8)}×  "
      f"ATR≤{filters.get('max_atr_pct',10)}%")
    # BUG FIX (audit finding, 2026-07-18): this table used to show nothing
    # when every ticker crashed inside analyze() — "0 passed" from a total
    # pipeline failure looked pixel-identical to "0 passed" from a genuinely
    # quiet market with no qualifying setups. screen() already computes
    # total_errors; it just never reached this view.
    if total_err:
        a(f"  ⚠️  {total_err} ticker(s) errored during scan — see scan_errors / --why for detail")
    if macro_warn:
        a(f"  {macro_warn}")
    a(SEP)

    if not picks:
        a("  No picks passed all filters.")
        a(SEP)
        print("\n".join(lines))
        return

    # Column header
    a(f"  {'#':>2}  {'TICKER':<5}  {'SCR':>4}  {'C':>1}  {'REGIME':<11}  {'STRATEGY':<16}  {'PRICE':>14}  {'SL':>6}  {'TP':>6}")
    a(f"  {DIV}")

    for p in picks:
        rank     = p.get("rank", "?")
        ticker   = p.get("ticker", "?")
        score    = p.get("score", 0)
        conf     = p.get("confidence", "")
        strategy = p.get("strategy", "N/A")
        regime   = p.get("regime", "")
        price    = _usd(p.get("price", "N/A"))
        sl_pct   = p.get("sl_pct", "?")
        tp_pct   = p.get("tp2_pct", "?")
        warnings = p.get("warnings", [])
        signals  = p.get("top_signals", [])
        htf      = p.get("htf_bias", "")

        grade      = conf[0] if conf else "?"
        score_str  = f"+{score}" if score > 0 else str(score)
        reg_short  = (regime
                      .replace("TRENDING_UP",    "TREND↑")
                      .replace("TRENDING_DOWN",  "TREND↓")
                      .replace("VOLATILE_AVOID", "VOLATILE!")
                      [:11])
        strat_short = strategy[:16]
        warn_tag    = " ⚠" if warnings else ""
        htf_tag     = " ↓HTF" if htf == "bearish" else ""

        a(f"  {rank:>2}  {ticker:<5}  {score_str:>4}  {grade}  {reg_short:<11}  "
          f"{strat_short:<16}  {price:>14}  {sl_pct:>6}  {tp_pct:>6}{warn_tag}{htf_tag}")

        # Key signals (top 2, truncated)
        if signals:
            sig_line = "  │  ".join(s[:28] for s in signals[:2])
            a(f"       Signals: {sig_line}")

        # Warnings
        for w in warnings:
            a(f"       ⚠  {w}")

        a("")  # blank row between picks

    a(SEP)

    # Filtered-out summary (only when --why was used)
    if rejected:
        a(f"  FILTERED ({len(rejected)} rejected):")
        for r in rejected[:6]:
            t    = r.get("ticker", "?")
            why  = r.get("_why_rejected", "?")
            sc   = r.get("score", "?")
            stg  = r.get("strategy", "")
            a(f"    ✗  {t:<6}  score={sc:<4}  {stg:<16}  → {why}")
        if len(rejected) > 6:
            a(f"    … and {len(rejected)-6} more (run with --why to see all)")
        a(SEP)

    print("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MadBot Market Screener v2.6")
    # BUG FIX (audit finding, 2026-07-14): default was "all", meaning running
    # `screener.py` with no argument silently scanned IDX stocks alongside
    # crypto — contradicting the crypto-only scope decided earlier in this
    # project. Not deleting the stock path (fetch_dynamic_stock_watchlist,
    # IDX_UNIVERSE stay intact) since that's a bigger, harder-to-reverse
    # change — just no longer the silent default. Explicit `py screener.py
    # stock` or `py screener.py all` still works exactly as before.
    parser.add_argument("asset_type", nargs="?", default="crypto", choices=["crypto", "stock", "all"])
    parser.add_argument("--timeframe", "-tf", default=None)
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--min-vol",   type=float, default=None)
    parser.add_argument("--max-atr",   type=float, default=None)
    parser.add_argument("--top",       type=int, default=None)
    parser.add_argument("--no-cache",  action="store_true")
    parser.add_argument("--why",       action="store_true")
    parser.add_argument("--workers",   type=int, default=None)
    parser.add_argument("--debug",     action="store_true")
    # N7: compact output — agent-optimized, suppresses INFO spam, no indent, strips metadata
    parser.add_argument("--json",      action="store_true", help="Raw JSON output instead of formatted table")
    parser.add_argument(
        "--compact", "-c", action="store_true",
        help=(
            "Agent-optimized output: suppress INFO log lines (pty captures stderr → token waste), "
            "use separators instead of indent=2, strip internal metadata fields. "
            "ZERO effect on signal logic or picks structure."
        ),
    )
    args = parser.parse_args()

    # N7: In compact mode, silence INFO logs early — before load_config() calls log.info()
    # Rationale: pty mode in OpenClaw captures stderr alongside stdout. Every INFO line
    # (e.g. "✓ ASTER PASSED score=6" × 71 tickers) is sent back as part of the tool result,
    # wasting context tokens. Compact mode keeps only WARNING+ (errors worth seeing).
    if args.compact:
        logging.getLogger().setLevel(logging.ERROR)
        logging.getLogger("screener").setLevel(logging.ERROR)
        logging.getLogger("analyze").setLevel(logging.ERROR)
        # also kill yfinance / urllib3 noise
        logging.getLogger("yfinance").setLevel(logging.ERROR)
        logging.getLogger("urllib3").setLevel(logging.ERROR)
        logging.getLogger("peewee").setLevel(logging.ERROR)

    if args.debug:
        logging.getLogger("screener").setLevel(logging.DEBUG)

    cfg = load_config()
    if args.timeframe: cfg["timeframe"]     = args.timeframe
    if args.min_score: cfg["min_score"]     = args.min_score
    if args.min_vol:   cfg["min_vol_ratio"] = args.min_vol
    if args.max_atr:   cfg["max_atr_pct"]  = args.max_atr
    if args.workers:   cfg["max_workers"]   = args.workers
    if args.top:       cfg["top_picks"]     = args.top   # sets results shown, NOT universe size

    try:
        if args.asset_type == "all":
            output = {
                "crypto":    screen("crypto", cfg, args.no_cache, args.why),
                "stock":     screen("stock", cfg, args.no_cache, args.why),
                "scan_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
        else:
            output = screen(args.asset_type, cfg, args.no_cache, args.why)

        # Output — pretty table by default, raw JSON with --json flag
        if args.compact:
            def _strip_metadata(d: dict) -> dict:
                """Remove internal fields that waste agent context. Picks structure unchanged."""
                d.pop("_from_cache", None)
                d.pop("scan_errors", None)    # duplicates total_errors
                d.pop("rejected_detail", None) # always [] in normal mode
                return d

            if isinstance(output, dict):
                if "crypto" in output and "stock" in output:
                    _strip_metadata(output.get("crypto", {}))
                    _strip_metadata(output.get("stock", {}))
                else:
                    _strip_metadata(output)
            print(json.dumps(output, separators=(",", ":"), ensure_ascii=False, default=str))

        elif args.json:
            print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

        else:
            # Human-readable table (default)
            if isinstance(output, dict) and "crypto" in output and "stock" in output:
                pretty_print_screener(output["crypto"])
                pretty_print_screener(output["stock"])
            else:
                pretty_print_screener(output)
            # Optionally also dump JSON when --why is used (diagnostic context)
            if args.why:
                print("\n── raw JSON (--why mode) ──")
                print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

    except KeyboardInterrupt:
        print(json.dumps({"error": "Interrupted", "picks": []}))
    except Exception as e:
        log.exception("Top-level crash")
        print(json.dumps({"error": str(e), "picks": []}))
