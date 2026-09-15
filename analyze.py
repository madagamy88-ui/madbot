"""
analyze.py — MadBot Signal Analyzer v7.2

Trade-direction fix (2026-07-29 — the most severe finding to date, found by
hand-verifying real trade_setup numbers against real signals from an actual
run, not by reading code in isolation):
  size_position() never took score/direction as an input at all — it
       unconditionally built a LONG-style entry/SL/TP (SL below entry, TP
       above) for every single call, regardless of signal bias. Confirmed
       against real output from an actual run: ETH (score=-4, tradeable=
       true, BEARISH) and BBCA (score=-9, tradeable=true, BEARISH) both
       produced a full, real-looking long entry/SL/TP — in both the raw
       JSON AND the human-readable pretty_print() CLI table, not just an
       internal field. get_entry_zone(), a few hundred lines below, already
       treats score<=-2 as NO_LONG ("avoid longs", no aggressive/
       conservative zone) — size_position() was silently contradicting that
       exact, already-established convention two fields away in the same
       output dict. This codebase has no short-selling capability anywhere
       (get_entry_zone's NO_LONG branch, TIER_LABELS' "NO LONG TRADES"), and
       counter_trend_valid — the one path that produces a LONG setup during
       a downtrend — is only ever True when sc > 0, so there's no
       legitimate case where a negative score should receive a real
       entry/SL/TP. Fix: size_position() now takes `score`, and returns
       an error dict ("No long setup — bearish bias, avoid longs") for
       score<=-2 instead of a real entry/SL/TP — NOT a flipped short
       template, since fabricating a trade type this system can't execute
       would be worse than the original bug. Verified four ways: reproduced
       the exact ETH and BBCA numbers from the log and confirmed both now
       correctly return the error dict; confirmed a real bullish case
       (score=2) is completely unaffected; confirmed the old default
       (score=0) doesn't accidentally suppress everything; and ran the
       actual pretty_print() function end-to-end, which turned out to
       already have a clean "No trade — bearish signal, do not long"
       message built for exactly this case — it just wasn't reachable
       before, since size_position() never returned an error for bearish
       signals to trigger it.

Streamlit-readiness fixes, round 2 (2026-07-24 — ccxt thread-safety,
confirmed via a ccxt maintainer's own response, not assumed):
  ccxt's synchronous Exchange class is not thread-safe for its own internal
       rate limiter — confirmed directly by a ccxt maintainer
       (github.com/ccxt/ccxt/issues/26968, Oct 2025): concurrent calls on the
       SAME exchange instance can race on the shared lastRestRequestTimestamp
       attribute. screener.py's ThreadPoolExecutor(max_workers=4) calls
       analyze() concurrently, and every call funnels through fetch_ohlcv()
       below, hitting the same _BINANCE_EX / _INDODAX_EX singleton — exactly
       the scenario confirmed racy. screener.py's existing _throttle() only
       paces when each thread's analyze() call STARTS, not how long ccxt is
       actually mid-call, so overlap was still possible whenever one call ran
       longer than inter_ticker_delay. Added _BINANCE_CALL_LOCK /
       _INDODAX_CALL_LOCK, scoped to just the actual network call in
       fetch_ohlcv() (not the cache lookup or DataFrame processing around
       it, and not cross-exchange — Binance and Indodax calls stay
       independent of each other since they don't share throttler state).
       Verified with a real concurrency test: 6 threads hammering the same
       mocked exchange object never had more than 1 inside the call at once
       after the fix; Binance and Indodax calls confirmed to still run
       concurrently with each other, not needlessly serialized. Real,
       known cost: this does trade away some wall-clock concurrency on the
       network-bound portion specifically, in exchange for actually closing
       a maintainer-confirmed race instead of hoping dispatch spacing is
       wide enough in practice.
  GAP FOUND ON RE-AUDIT, same day: the first pass above missed that
       fetch_rate()'s own Indodax ticker call was guarded by _RATE_LOCK
       only, a DIFFERENT lock than _INDODAX_CALL_LOCK — meaning it could
       still race against fetch_ohlcv()'s Indodax fallback path on the same
       _INDODAX_EX object, just through a narrower window than the original
       Binance case. Closed by having fetch_rate() share _INDODAX_CALL_LOCK
       too. Verified no deadlock with a real 8-thread test mixing both call
       paths simultaneously with a hard timeout, and confirmed max
       concurrent same-exchange calls is exactly 1 across both paths
       together, not just within each path separately.

Streamlit-readiness fixes (2026-07-23 — audit finding, each verified by
actually running the code, not just read and asserted correct; zero
scoring/signal logic touched):
  Import-order logging race — logging.basicConfig() configures the ROOT
       logger, and only the first call in a process has any effect; this
       file's call and screener.py's own call were silently fighting over
       which one "won" depending on which file got imported first. Invisible
       as a CLI script; would silently discard whichever file's intended log
       level lost the race under a Streamlit app importing both. Guarded
       behind `if __name__ == "__main__"` — CLI behavior unchanged (verified:
       identical root-logger level regardless of import order now), library-
       import behavior no longer touches global state.
  sys.stderr race in fetch_stock() — sys.stderr is a single process-global
       object; the existing swap-to-devnull-then-restore pattern around
       yf.download() calls has no protection against two threads doing this
       concurrently (e.g. two Streamlit sessions both calling fetch_stock()
       at once). Traced the exact interleaving: one thread can restore
       stderr to DEVNULL instead of the real stderr, permanently silencing
       all stderr output — including this file's and screener.py's own
       logging — for the rest of the process, until restart. Added
       _STDERR_REDIRECT_LOCK around the swap+restore, making it atomic
       across threads. Costs some parallelism on the yfinance leg
       specifically; correctness there matters more than that.

Infrastructure fixes (2026-07-19 — audit finding, robustness pass; zero
scoring/signal logic touched, verified by diff):
  ccxt timeout — _get_binance()/_get_indodax() never set an explicit
       timeout, silently falling back to ccxt's built-in 10000ms default
       (verified against ccxt's own docs). screener.py's own F4 changelog
       already established Indonesian ISP routing to Binance needs closer
       to 20s. Set explicitly to 20000ms on both singletons.
  yfinance timeout — fetch_stock()'s yf.download() call relied on
       yfinance's own unstated default (verified: yfinance.download()
       signature defaults timeout=10, in seconds not ms). Made explicit at
       20s for the same ISP-latency reasoning as the ccxt fix, not because
       the old behavior could hang indefinitely — it couldn't, the default
       already bounded it.
  force_refresh threading — analyze() had no way to bypass its own OHLCV
       disk cache (_ohlcv_cache_get/_ohlcv_cache_set, independent TTLs:
       600s/1H, 1800s/4H, 14400s/1D). screener.py's --no-cache flag only
       ever reached its OWN scan-level cache; this file's candle data could
       silently still be cache-age-old on a "fresh" run with zero
       indication it happened. Added force_refresh param to analyze(),
       get_mtf_alignment(), threaded through every fetch_ohlcv() call in
       the crypto path (entry TF, SMA200 gate's 1D fetch, MTF compass/
       engine fetches). fetch_stock() untouched — it has no disk cache
       layer, nothing for force_refresh to bypass there.
  All three fixes are additive-only (new optional kwargs, default
  unchanged) — verified backward compatible with any existing caller
  (backtest.py included) that doesn't pass the new arguments.

v7.2 changes (2026-07-09 — Signal Normalization + Regime-Native Routing +
AVWAP, built and tested in isolation before integration, verified against
4 realistic end-to-end scenarios including the exact BTC-4H-below-SMA200-
tight-SQUEEZE case that drove the entire v7.1 debugging effort):

  REPLACED: generate_signal()'s flat additive scoring (~24 discrete
  `sc += 1` lines, order-dependent, double/triple-counting correlated
  signals as if independent) with four normalized [-1,+1] components —
  trend, momentum, volume, structure — each collapsing multiple correlated
  raw indicators into ONE reading via averaging (not summing) before it's
  weighted. This is the actual fix for multicollinearity, verified twice
  independently: EMA9/21 cross + price-vs-EMA50 + price-vs-VWAP +
  price-vs-AVWAP were previously 4 separate +1's for what is substantially
  one underlying fact (price is above its recent average, measured 4 ways).

  FIXED (found during build, not before): the volume component's initial
  draft had a sign error — "OBV confirms a bearish trend" was scored as a
  POSITIVE vote instead of negative. Caught by scenario-testing a clean
  downtrend before integration (it scored -2, should have been strongly
  negative); fixed and re-verified symmetric with the clean-uptrend case
  (+8 / -8) before this ever touched the live file.

  ADDED: regime-native routing. `regime` (from detect_regime(), unchanged)
  now decides EACH component's weight before scoring, not an end-of-
  pipeline multiplier applied after the fact. TRENDING_UP/DOWN ignore
  oscillators (unreliable mid-trend). SQUEEZE leans on volume+structure
  (a breakout needs real participation; EMAs are tangled in a squeeze by
  definition). RANGING leans on momentum+structure (mean-reversion IS the
  range signal; trend is meaningless in a range by definition).

  ADDED: AVWAP (Anchored VWAP), direction-aware capitulation/climax anchor.
  BUG FIX vs the originally proposed design: anchoring purely to the
  highest-volume bar in a lookback can't distinguish a capitulation BOTTOM
  from a blow-off TOP — a volume spike happens at both. Fixed: only anchors
  when that bar is ALSO a genuine local price extreme (low in bottom 20% of
  the lookback range -> bullish anchor; high in top 20% -> bearish anchor).
  Verified against synthetic bottom/top/mid-range test cases — mid-range
  spikes (neither extreme) correctly produce no anchor rather than a guess.
  SMA200 was NOT replaced — kept as-is (canonical, zero-subjectivity macro
  filter); AVWAP is a new, additive entry-timeframe trend-component input.

  UNCHANGED, verbatim, because it was independently verified correct and
  was never what the multicollinearity/regime-routing work was about: the
  SMA200 graduated-reduction gate (floor of 1, never a hard zero — the
  fix this whole project centered on), the ADX/HTF macro gate, mr_confirms,
  the HTF/BTC post-scoring overlays, the magnitude-based confidence formula,
  and the full return-dict shape (pretty_print()/screener.py/backtest.py
  compatibility preserved).

Prior changelog (v7.0/v7.1 — Phase 2 rebuild, bug-fix audit) preserved in
git history / prior file versions; trimmed here to keep this header current.

Usage:
  python analyze.py BTC           # pretty print, 1H
  python analyze.py BTC 4H
  python analyze.py BBCA 1D       # IDX stock
  python analyze.py BTC --raw     # JSON output
  python analyze.py BTC --no-mtf  # skip 3-TF fetch (screener batch mode)
"""

# ── SILENCE EVERYTHING before any import ─────────────────────────────────────
import sys, os, warnings, logging, threading

# BUG FIX (audit finding, 2026-07-23): logging.basicConfig() configures the
# ROOT logger, and only the first call in a process has any effect — every
# later basicConfig() call anywhere else (e.g. screener.py's own call) is a
# silent no-op. As a CLI script that's invisible; the moment both this file
# and screener.py are imported into one long-lived process (a Streamlit app
# importing both), whichever gets imported first wins and the other file's
# intended log level is silently discarded. Guarding this behind __main__
# means CLI usage (python analyze.py ...) is byte-for-byte unchanged
# (__name__ == "__main__" there), while importing this module as a library
# no longer touches global state. The app entrypoint that eventually imports
# both analyze and screener should configure the root logger itself, once,
# before either import.
if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

for _lib in ("ccxt","urllib3","requests","yfinance","peewee",
             "curl_cffi","charset_normalizer","PIL","pandas"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)

import json, time, pathlib
import pandas as pd

try:
    import pandas_ta_classic as ta
except ImportError:
    import pandas_ta as ta

import ccxt, requests

# UTF-8 stdout (Windows cp1252 fix)
import io as _io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Redirect stderr for yfinance noise
_DEVNULL = open(os.devnull, "w")
# BUG FIX (audit finding, 2026-07-23): sys.stderr is a single process-global
# object. Two threads swapping it concurrently (e.g. two Streamlit sessions
# both calling fetch_stock() at once) can interleave such that one thread
# restores stderr to DEVNULL instead of the real stderr, permanently
# silencing all stderr output — including this file's and screener.py's own
# logging — for the rest of the process, until restart. This lock forces the
# swap+restore to be atomic across threads. Costs some parallelism on the
# yfinance leg specifically; correctness here matters more than that.
_STDERR_REDIRECT_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — CONSTANTS & HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
TOTAL_CAP   = float(os.environ.get("TOTAL_CAPITAL", 27_500_000))
RISK_PCT    = float(os.environ.get("RISK_PCT", 0.02))
CRYPTO_CAP  = TOTAL_CAP * 0.60
STOCK_CAP   = TOTAL_CAP * 0.40
W = 66  # box width

CRYPTO_TICKERS = {
    "BTC","ETH","SOL","BNB","DOGE","ADA","XRP","MATIC","AVAX","DOT","LINK",
    "UNI","ATOM","NEAR","APT","TRX","LTC","BCH","ETC","XLM","ALGO","SAND",
    "MANA","FTM","AAVE","ARB","OP","SUI","TON","PEPE","SHIB","INJ","FET",
    "WIF","BONK","JUP","PYTH","SEI","TIA","BLUR","GMX","DYDX","LDO","CRV",
    "SNX","COMP","MKR","ZEC","ZRX","BAT","ENJ","CHZ","FLOW","ICP","HBAR",
    "VET","THETA","GRT","EGLD","ONE","ROSE","CELO","SKL","LUNC","PENGU",
    "OSMO","ONDO","MORPHO","SAHARA","WLD","USD1","CELR","HOT","AMP",
}

# base_tf → (compass, engine, trigger)
MTF_LAYERS = {
    "1m":  ("4H",  "1H",  "15m"),
    "5m":  ("4H",  "1H",  "15m"),
    "15m": ("1D",  "4H",  "1H"),
    "1H":  ("1D",  "4H",  "1H"),
    "4H":  ("1W",  "1D",  "4H"),
    "1D":  ("1W",  "1D",  "1D"),
    "1W":  ("1W",  "1W",  "1W"),
}

TF_NORM = {
    "1m":"1m","5m":"5m","15m":"15m",
    "1h":"1H","4h":"4H","1d":"1D","1w":"1W",
    "1H":"1H","4H":"4H","1D":"1D","1W":"1W",
}

# Bar duration in seconds per timeframe key — used to detect and drop a
# still-forming (not yet closed) candle at the tail of a fetched OHLCV series.
TF_SECONDS = {"1m":60,"5m":300,"15m":900,"1H":3600,"4H":14400,"1D":86400,"1W":604800}

# F6 — Timeframe-aware regime thresholds.
REGIME_THRESHOLDS = {
    "1m":  {"atr_avoid": 20.0, "slope_flat": 0.05, "slope_trend": 0.12, "bb_squeeze": 0.015},
    "5m":  {"atr_avoid": 18.0, "slope_flat": 0.08, "slope_trend": 0.18, "bb_squeeze": 0.018},
    "15m": {"atr_avoid": 15.0, "slope_flat": 0.10, "slope_trend": 0.22, "bb_squeeze": 0.020},
    "1H":  {"atr_avoid": 12.0, "slope_flat": 0.15, "slope_trend": 0.30, "bb_squeeze": 0.025},
    "4H":  {"atr_avoid":  8.0, "slope_flat": 0.25, "slope_trend": 0.50, "bb_squeeze": 0.030},
    "1D":  {"atr_avoid":  5.0, "slope_flat": 0.40, "slope_trend": 0.80, "bb_squeeze": 0.040},
    "1W":  {"atr_avoid":  4.0, "slope_flat": 0.60, "slope_trend": 1.20, "bb_squeeze": 0.050},
}

# F7 — Timeframe-aware pivot lookback for find_structure().
TF_STRUCTURE_N = {
    "1m": 3, "5m": 3, "15m": 4,
    "1H": 5, "4H": 7, "1D": 10, "1W": 5,
}

# F9 — Thread-safe module-level caches
_RATE_CACHE: dict = {"rate": None, "ts": 0.0, "src": "fallback"}
_RATE_LOCK  = threading.Lock()
_BTC_CACHE:  dict = {"bias": "unknown", "ts": 0.0}
_BTC_LOCK   = threading.Lock()

# F8 — Session cache for asset-type detection
_ASSET_TYPE_CACHE: dict = {}
_ASSET_LOCK = threading.Lock()   # Bug 2 fix — guards concurrent writes

# ── P2-A: ccxt exchange singletons ──────────────────────────────────────────
# One exchange object per process, reusing the same HTTP connection pool.
# Previously: new ccxt.binance() created on every fetch_ohlcv() call, causing
# a fresh TLS handshake for each call — catastrophic through the Indonesian ISP
# DNS tunnel. Double-checked locking pattern matches _BTC_LOCK / _RATE_LOCK.
_BINANCE_EX  = None
_BINANCE_LOCK = threading.Lock()
_INDODAX_EX  = None
_INDODAX_LOCK = threading.Lock()

# BUG FIX (audit finding, 2026-07-24 — confirmed via ccxt maintainer
# response, not assumed): ccxt's synchronous Exchange class is NOT
# thread-safe for its own internal rate limiter. A ccxt maintainer confirmed
# this directly (github.com/ccxt/ccxt/issues/26968, Oct 2025): "if using the
# same exchange across two threads there is a chance two threads could edit
# the throttler simultaneously, causing an issue" — the shared
# lastRestRequestTimestamp attribute on the Exchange instance has no
# synchronization of its own. screener.py's ThreadPoolExecutor(max_workers=4)
# calls analyze() concurrently across threads, and every one of those calls
# eventually reaches fetch_ohlcv() below, which hits the SAME _BINANCE_EX /
# _INDODAX_EX singleton — exactly the scenario the maintainer confirmed is
# racy. The reporter's own real-world experience was that this manifests as
# rate limiting being applied MORE aggressively (wasted time), not bans —
# still worth closing rather than accepting, since screener.py's own
# existing _throttle() only paces when each thread's analyze() call STARTS,
# not how long ccxt is actually mid-call, so overlap is still very possible
# whenever one call takes longer than inter_ticker_delay (default 0.3s).
# These locks serialize only the actual network call on each exchange
# object — not the surrounding cache lookup or DataFrame processing, and not
# calls to the OTHER exchange (Binance and Indodax calls remain independent
# of each other; only same-exchange concurrent calls were ever racy). This
# does trade away some wall-clock concurrency on the network-bound portion
# specifically — a real, known cost, not a free fix — in exchange for
# actually eliminating a maintainer-confirmed race instead of just hoping
# _throttle()'s dispatch spacing is wide enough in practice.
_BINANCE_CALL_LOCK = threading.Lock()
_INDODAX_CALL_LOCK = threading.Lock()

# BUG FIX (Finding T): guards the signal-logger's check-then-act sequence
# (does the file exist? -> open and maybe write a header) against the race
# condition proven with real concurrent threads matching screener.py's actual
# default of 4 workers — without this lock, multiple threads can each see
# "file doesn't exist yet" and each write their own header row, corrupting
# the CSV with duplicate headers interleaved through the data.
_SIGNAL_LOG_LOCK = threading.Lock()

# ── DASHBOARD FIX (Step 1 — resilience) ──────────────────────────────────────
# Every network call in this file previously got exactly one attempt: fail,
# and the caller's existing try/except moves straight to the next fallback
# (Binance -> Indodax -> give up). That means a one-second transient blip and
# a genuine multi-hour outage were indistinguishable — both produced an
# identical single-attempt failure, and a dead exchange got hit just as hard
# on the NEXT call as the first one.
#
# This generalizes a pattern that already exists in exactly one place in this
# codebase — screener.py's fetch_binance_valid_symbols() already fails fast
# for 60s after a failure instead of re-attempting a blocking call every
# time. That's a real circuit breaker, just local to one function. Here it's
# shared across every exchange/API dependency this file calls:
#   - one short-backoff retry absorbs a transient blip
#   - a cooldown window (circuit "open") stops hammering a dependency that
#     just failed, until the cooldown elapses
# Keyed by DEPENDENCY (binance / indodax / finnhub / yfinance), not by call
# site — Indodax's OHLCV fallback and its rate-ticker call share one circuit,
# since a real Indodax outage should back off both, not just one.
_CIRCUIT_STATE: dict = {}   # {dependency_key: last_failure_unix_ts}
_CIRCUIT_LOCK   = threading.Lock()
_CIRCUIT_COOLDOWN = 60  # seconds — same window screener.py already uses

def _circuit_open(key: str) -> bool:
    ts = _CIRCUIT_STATE.get(key)
    return ts is not None and (time.time() - ts) < _CIRCUIT_COOLDOWN

def _circuit_record_failure(key: str):
    with _CIRCUIT_LOCK:
        _CIRCUIT_STATE[key] = time.time()

def _circuit_record_success(key: str):
    if key in _CIRCUIT_STATE:
        with _CIRCUIT_LOCK:
            _CIRCUIT_STATE.pop(key, None)

def _resilient_call(key: str, fn, retries: int = 1, backoff: float = 1.0):
    """Run fn() with one short-backoff retry on failure. If `key` is
    already in its cooldown window from a recent failure, fails immediately
    with no call at all — same fail-open contract as before (caller's
    existing except block still catches this and moves to its fallback),
    just no longer wasting a full timeout re-attempting a dependency that
    JUST failed. Deliberately small: 1 retry, 1s backoff — this is a
    personal single-user tool, not a high-throughput service; the goal is
    surviving a blip, not building a full resilience framework.

    BUG FIX (audit finding, 2026-09): a single unlisted/bad ticker symbol
    (confirmed real example in a live scan log: a malformed non-ASCII
    "ticker" that should never have reached this function at all — see the
    matching screener.py fix for where that should be filtered upstream)
    was tripping the SHARED "binance" circuit for the full 60s cooldown,
    which then fail-fast-rejected every OTHER ticker in that scan window
    regardless of whether THEIR symbol was fine — confirmed by scan timing
    in the log (28 tickers, most reporting "Could not fetch" in ~7 seconds
    total, far too fast for 18 genuine 20s network timeouts; consistent
    with most of them being short-circuited by an already-open breaker, not
    each independently failing). A bad-symbol error is a permanent, ticker-
    specific fact — it will never resolve by waiting out a cooldown — so it
    should fail immediately for THIS ticker without punishing every other
    one sharing the same circuit key. ccxt.SymbolNotFound does NOT exist in
    the installed ccxt version (4.5.78) — verified directly, not assumed;
    only ccxt.BadSymbol is real. Kept the string-match fallback since not
    every exchange/ccxt version raises BadSymbol consistently for this.
    """
    if _circuit_open(key):
        raise RuntimeError(f"{key}: skipped — in {_CIRCUIT_COOLDOWN}s cooldown after a recent failure")
    last_exc = None
    for attempt in range(retries + 1):
        try:
            result = fn()
            _circuit_record_success(key)
            return result
        except Exception as e:
            last_exc = e
            err_msg = str(e).lower()
            is_bad_symbol = (
                isinstance(e, ccxt.BadSymbol) or
                any(term in err_msg for term in
                    ["badsymbol", "symbol not found", "does not have market symbol", "invalid symbol"])
            )
            if is_bad_symbol:
                # Permanent, ticker-specific — raise immediately, do NOT
                # record a circuit failure that would poison every other
                # ticker sharing this dependency key.
                raise e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    _circuit_record_failure(key)
    raise last_exc


def _get_binance():
    global _BINANCE_EX
    if _BINANCE_EX is None:
        with _BINANCE_LOCK:
            if _BINANCE_EX is None:
                # BUG FIX (audit finding, 2026-07-19): no explicit timeout was
                # set here, so ccxt fell back to its built-in default of
                # 10000ms (verified against ccxt's own docs/manual — this is
                # not a Binance-specific value, it's ccxt's global default).
                # This codebase already established elsewhere (see
                # screener.py's F4 changelog) that Indonesian ISP routing to
                # Binance needs closer to 20s than 10s. That fix never made
                # it into this singleton. ccxt's own docs recommend an
                # explicit 10-30s range for production rather than relying on
                # the default. Using 20000ms to match the existing precedent.
                _BINANCE_EX = ccxt.binance({
                    "options": {"defaultType": "spot"},
                    "enableRateLimit": True,
                    "timeout": 20000,
                })
    return _BINANCE_EX


def _get_indodax():
    global _INDODAX_EX
    if _INDODAX_EX is None:
        with _INDODAX_LOCK:
            if _INDODAX_EX is None:
                # Same fix as _get_binance() above — same missing timeout,
                # same fallback to ccxt's 10s default, same reasoning.
                _INDODAX_EX = ccxt.indodax({"timeout": 20000})
    return _INDODAX_EX


def sf(v):
    """Safe float — returns None on NaN/None/error."""
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except Exception:
        return None


def dual(val, rate=None):
    """Format IDR value with optional USD equivalent.
    Handles sub-rupiah meme coins (SHIB/PEPE/BONK) that previously showed Rp 0 ($0.00).
    """
    if val is None:
        return "N/A"
    v = float(val)

    # ── IDR display ──────────────────────────────────────────────────────────
    if v >= 1_000:
        idr_str = f"Rp {int(v):,}"
    elif v >= 1:
        idr_str = f"Rp {v:,.2f}"
    elif v > 1e-9:
        idr_str = f"Rp {v:.4g}"
    else:
        idr_str = "Rp 0"

    if not rate:
        return idr_str

    # ── USD display ──────────────────────────────────────────────────────────
    usd = v / rate
    if usd >= 1_000:
        usd_str = f"${usd:,.2f}"
    elif usd >= 0.01:
        usd_str = f"${usd:,.4f}" if usd < 1 else f"${usd:,.2f}"
    elif usd > 1e-9:
        raw = f"{usd:.8f}".rstrip("0")
        usd_str = f"${raw}"
    else:
        usd_str = f"${usd:.2e}"

    return f"{idr_str} ({usd_str})"


def _to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")


def _drop_forming_candle(df, tf_key):
    """
    Bug (found in live output): both ccxt (Binance/Indodax) and yfinance
    return the currently-forming, not-yet-closed candle as the last row when
    queried mid-period. That row's volume is only a partial-period sample —
    e.g. requesting a 4H bar 2 minutes after it opened yields ~2 minutes of
    volume compared against a 20-bar average of FULL 4H volumes, producing
    an artificial "vol ratio ~0.00x" that silently guts every vol_strong-gated
    signal (Donchian breakout, engulfing, MFI quality) for most of that bar's
    life. It also feeds calc_indicators() an unstable, still-moving close for
    EMA/RSI/MACD, which can flip cross signals before the bar actually closes.
    Drop the last row if its period hasn't fully elapsed. Index is the
    candle's OPEN timestamp (UTC, tz-naive for ccxt; sometimes tz-aware for
    yfinance, normalized below).
    """
    secs = TF_SECONDS.get(tf_key)
    if not secs or df.empty:
        return df
    last_open = df.index[-1]
    if getattr(last_open, "tzinfo", None) is not None:
        last_open = last_open.tz_convert("UTC").tz_localize(None)
    close_time = last_open + pd.Timedelta(seconds=secs)
    now_utc = pd.Timestamp.utcnow().tz_localize(None)
    if now_utc < close_time:
        return df.iloc[:-1]
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — DATA FETCH
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_rate(ttl=300):
    # Fast path — no lock needed for read
    if _RATE_CACHE["rate"] and (time.time() - _RATE_CACHE["ts"]) < ttl:
        return _RATE_CACHE["rate"], _RATE_CACHE["src"]
    with _RATE_LOCK:
        if _RATE_CACHE["rate"] and (time.time() - _RATE_CACHE["ts"]) < ttl:
            return _RATE_CACHE["rate"], _RATE_CACHE["src"]
        try:
            # BUG FIX (audit finding, 2026-07-24, caught on re-audit after
            # the ccxt lock fix — the first pass missed this): this call and
            # fetch_ohlcv()'s Indodax fallback both hit the same _INDODAX_EX
            # object, but were guarded by two DIFFERENT locks (_RATE_LOCK
            # here, _INDODAX_CALL_LOCK there) — meaning they could still race
            # against each other on the shared throttler state even after
            # the ccxt fix, just through a narrower path than the original
            # Binance-vs-Binance case. Sharing _INDODAX_CALL_LOCK here closes
            # that gap. Safe to nest inside _RATE_LOCK: nothing under
            # _INDODAX_CALL_LOCK ever tries to acquire _RATE_LOCK, so there's
            # no circular wait.
            with _INDODAX_CALL_LOCK:
                rate = round(_resilient_call("indodax", lambda: _get_indodax().fetch_ticker("USDT/IDR")["last"]), 0)
            _RATE_CACHE.update({"rate": rate, "ts": time.time(), "src": "live Indodax"})
            return rate, "live Indodax"
        except Exception:
            fb = 16200.0
            _RATE_CACHE.update({"rate": fb, "ts": time.time(), "src": "fallback"})
            return fb, "fallback"


def fetch_live_price(ticker: str) -> float | None:
    """
    F4 — Lightweight Binance ticker call (~100-300ms) for the actual current price.
    Called AFTER the OHLCV fetch so indicators are unaffected.
    Only used for display: entry price, SL, TP in the output.
    Returns None on any failure — caller falls back to OHLCV close.
    """
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": f"{ticker.upper()}USDT"},
            timeout=5,
        )
        r.raise_for_status()
        price = float(r.json()["price"])
        return price if price > 0 else None
    except Exception:
        return None


def _detect_asset_type(ticker: str) -> str:
    """
    F8 — Auto-detect crypto vs stock.
    Fast path: CRYPTO_TICKERS set (no network).
    Slow path: probe Binance /ticker/price for coins the screener found via CoinGecko
    that aren't in CRYPTO_TICKERS yet (new coins). Result cached for session.
    Bug 2 fix: _ASSET_LOCK prevents duplicate Binance probes from concurrent screener threads.
    """
    t = ticker.upper()
    if t in _ASSET_TYPE_CACHE:
        return _ASSET_TYPE_CACHE[t]
    if t in CRYPTO_TICKERS:
        _ASSET_TYPE_CACHE[t] = "crypto"
        return "crypto"
    with _ASSET_LOCK:
        if t in _ASSET_TYPE_CACHE:
            return _ASSET_TYPE_CACHE[t]
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": f"{t}USDT"},
                timeout=3,
            )
            result = "crypto" if r.status_code == 200 else "stock"
        except Exception:
            result = "stock"
        _ASSET_TYPE_CACHE[t] = result
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# OHLCV DISK CACHE — see fetch_ohlcv() docstring/comment for the full rationale.
# TTL scaled to each timeframe's own candle-close cadence: no point re-fetching
# 1D data every 2 minutes, it only changes once every 24h. Cross-process safe
# via the same exclusive-lockfile pattern used for the signal logger, since
# screener.py's threads AND separate manual `analyze.py` invocations can hit
# the same cache file at once. limit is part of the cache key — a caller
# asking for 300 bars must never silently receive a cached 80-bar entry.
# ═══════════════════════════════════════════════════════════════════════════════
_OHLCV_CACHE_DIR = pathlib.Path(__file__).resolve().parent / ".madbot_cache" / "ohlcv"

_OHLCV_CACHE_TTL = {   # seconds
    "1m": 30, "5m": 90, "15m": 300, "1H": 600, "4H": 1800, "1D": 14400, "1W": 43200,
}

def _cache_acquire_lock(path, timeout=3.0, stale_after=15.0):
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.remove(path); continue   # previous holder crashed — steal it
            except OSError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.02)

def _cache_release_lock(path):
    try: os.remove(path)
    except OSError: pass

def _ohlcv_cache_path(ticker, timeframe, limit):
    return _OHLCV_CACHE_DIR / f"{ticker.upper()}_{timeframe}_{limit}.pkl"

def _ohlcv_cache_get(ticker, timeframe, limit):
    """Returns a cached df if a fresh-enough entry exists, else None.
    Fails open on any error — a corrupt/unreadable cache entry just means
    'fetch fresh', never a crash."""
    ttl = _OHLCV_CACHE_TTL.get(timeframe, 600)
    fp = _ohlcv_cache_path(ticker, timeframe, limit)
    try:
        if not fp.exists():
            return None
        if time.time() - fp.stat().st_mtime > ttl:
            return None
        return pd.read_pickle(fp)
    except Exception:
        return None

def _ohlcv_cache_set(ticker, timeframe, limit, df):
    """Caching must never crash or block the caller — fail open, same
    convention as the signal logger's cross-process lock."""
    try:
        _OHLCV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fp = _ohlcv_cache_path(ticker, timeframe, limit)
        lock_fp = fp.with_suffix(".lock")
        if _cache_acquire_lock(lock_fp):
            try:
                df.to_pickle(fp)
            finally:
                _cache_release_lock(lock_fp)
    except Exception:
        pass


def fetch_ohlcv(ticker, timeframe, limit=300, force_refresh=False):
    # OHLCV DISK CACHE — added 2026-07-09 to address real Binance IP bans
    # (-1003 "Way too much request weight used; IP banned...") observed during
    # repeated manual testing + screener runs in short succession. EVERY
    # fetch_ohlcv() caller in this codebase — analyze(), get_btc_context(),
    # screener.py's 4-threaded scan across ~25-70 tickers, each needing
    # 1D+4H+1H (or 1W+1D+4H) for MTF alignment — funnels through this one
    # function, so caching here is the single highest-leverage fix; no other
    # file needs to change to benefit. get_btc_context()'s own 300s in-memory
    # cache (F9 fix) only survives within ONE process — it resets on every
    # fresh `analyze.py`/`screener.py` invocation, which is most of the real
    # load. This cache is on-disk and persists across invocations.
    # force_refresh=True bypasses it entirely for callers that need
    # guaranteed-fresh data regardless of TTL.
    if not force_refresh:
        cached = _ohlcv_cache_get(ticker, timeframe, limit)
        if cached is not None:
            return cached, None

    # P2-A: uses _get_binance() / _get_indodax() singletons — no per-call TLS handshake
    TF = {"1m":"1m","5m":"5m","15m":"15m","1H":"1h","4H":"4h","1D":"1d","1W":"1w"}
    tf = TF.get(timeframe)
    if not tf:
        return None, f"Unknown timeframe '{timeframe}'"
    sym = ticker.upper()
    try:
        with _BINANCE_CALL_LOCK:
            ohlcv = _resilient_call("binance", lambda: _get_binance().fetch_ohlcv(f"{sym}/USDT", tf, limit=limit))
        if ohlcv and len(ohlcv) >= 50:
            df = _to_df(ohlcv)
            df = _drop_forming_candle(df, timeframe)
            if len(df) >= 50:
                df.attrs.update(source="binance", quote="USDT")
                _ohlcv_cache_set(ticker, timeframe, limit, df)
                return df, None
    except Exception:
        pass
    try:
        with _INDODAX_CALL_LOCK:
            ohlcv = _resilient_call("indodax", lambda: _get_indodax().fetch_ohlcv(f"{sym}/IDR", tf, limit=limit))
        if ohlcv and len(ohlcv) >= 50:
            df = _to_df(ohlcv)
            df = _drop_forming_candle(df, timeframe)
            if len(df) >= 50:
                df.attrs.update(source="indodax", quote="IDR")
                _ohlcv_cache_set(ticker, timeframe, limit, df)
                return df, None
    except Exception:
        pass
    return None, f"Could not fetch {sym} — tried Binance and Indodax"


def fetch_stock(ticker, timeframe, limit=300):
    # Stocks: 4H not available anywhere — substitute 1D.
    # Bug 7 fix: track the actual TF used and return it as third value so
    # callers (analyze()) can update downstream functions (detect_regime,
    # find_structure) to use the correct thresholds instead of 4H thresholds
    # applied to 1D data.
    actual_tf = timeframe
    if timeframe == "4H":
        actual_tf = "1D"

    TF_FH   = {"1H":"60", "1D":"D", "1W":"W"}
    TF_SECS = {"1H":3600, "1D":86400, "1W":604800}

    if FINNHUB_KEY:
        try:
            r = _resilient_call("finnhub", lambda: requests.get(
                "https://finnhub.io/api/v1/stock/candle",
                params={
                    "symbol":     f"{ticker}.JK",
                    "resolution": TF_FH.get(actual_tf, "D"),
                    "from":       int(time.time()) - limit * TF_SECS.get(actual_tf, 86400),
                    "to":         int(time.time()),
                    "token":      FINNHUB_KEY,
                },
                timeout=10,
            ).json())
            if r.get("s") == "ok" and len(r.get("c", [])) > 20:
                df = pd.DataFrame(
                    {"open":r["o"],"high":r["h"],"low":r["l"],"close":r["c"],"volume":r["v"]},
                    index=pd.to_datetime(r["t"], unit="s"),
                )
                df.attrs.update(source="finnhub", quote="IDR")
                df = _drop_forming_candle(df, actual_tf)
                if len(df) > 20:
                    return df, None, actual_tf
        except Exception:
            pass

    try:
        import yfinance as yf
        with _STDERR_REDIRECT_LOCK:
            _old_stderr, sys.stderr = sys.stderr, _DEVNULL
            try:
                df = _resilient_call("yfinance", lambda: yf.download(
                    f"{ticker}.JK",
                    period="60d" if actual_tf == "1H" else "2y",
                    interval="1h" if actual_tf == "1H" else "1d",
                    progress=False,
                    auto_adjust=True,
                    timeout=20,   # was relying on yfinance's own default (10s) — made explicit, same reasoning as the ccxt fix above
                ))
            finally:
                sys.stderr = _old_stderr

        if len(df) < 20:
            raise ValueError("not enough data")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.attrs.update(source="yfinance", quote="IDR")
        df = _drop_forming_candle(df, actual_tf)
        if len(df) < 20:
            raise ValueError("not enough data after dropping forming candle")
        return df[["open","high","low","close","volume"]].tail(limit), None, actual_tf
    except Exception as e:
        return None, f"Stock fetch failed: {e}", actual_tf


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD FIX (Step 2 — data validation)
# calc_indicators()/find_structure()/generate_signal() below have never
# checked whether a fetched bar is actually sane before computing EMAs/RSI/
# score over it — a bad bar (high<low, a NaN price, a zero-or-negative
# close) would silently flow straight into the score with no signal
# anywhere that anything was wrong. Standard OHLC consistency rule: a bar's
# high must be >= its own open/low/close, and its low must be <= its own
# open/high/close. Run once, right after fetch, before any indicator ever
# touches the frame. Fail-open by design (never blocks analyze(), same
# convention as every other gate in this file) — this makes a bad bar
# VISIBLE, it doesn't refuse to proceed.
# ═══════════════════════════════════════════════════════════════════════════════
def validate_ohlcv(df, ticker: str = "", timeframe: str = "") -> list[str]:
    issues = []
    if df is None or df.empty:
        return issues
    cols = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in cols):
        return issues

    bad_high = df["high"] < df[cols].max(axis=1)
    bad_low  = df["low"]  > df[cols].min(axis=1)
    n_bad = int((bad_high | bad_low).sum())
    if n_bad:
        issues.append(
            f"{n_bad} bar(s) fail OHLC consistency (high<max or low>min) "
            f"— {ticker} {timeframe}, data may be corrupted"
        )

    n_nan = int(df[cols].isna().any(axis=1).sum())
    if n_nan:
        issues.append(f"{n_nan} bar(s) contain NaN price values — {ticker} {timeframe}")

    n_nonpos = int((df["close"] <= 0).sum())
    if n_nonpos:
        issues.append(f"{n_nonpos} bar(s) have non-positive close price — {ticker} {timeframe}")

    return issues


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════
def calc_indicators(df):
    if df.empty or len(df) < 50:
        return df
    c, h, l, v, o = df["close"], df["high"], df["low"], df["volume"], df["open"]

    df["ema9"]  = ta.ema(c, 9)
    df["ema21"] = ta.ema(c, 21)
    df["ema50"] = ta.ema(c, 50)
    df["rsi"]   = ta.rsi(c, 14)

    # F5 — Rolling VWAP (20-bar) instead of ta.vwap() which is cumulative from bar 0.
    _tp = (h + l + c) / 3
    df["vwap"] = (_tp * v).rolling(20).sum() / v.rolling(20).sum()

    stoch = ta.stoch(h, l, c, k=14, d=3, smooth_k=3)
    if stoch is not None:
        k_col = next((col for col in stoch.columns if "STOCHk" in col), None)
        d_col = next((col for col in stoch.columns if "STOCHd" in col), None)
        df["stoch_k"] = stoch[k_col] if k_col else None
        df["stoch_d"] = stoch[d_col] if d_col else None
    else:
        df["stoch_k"] = df["stoch_d"] = None

    macd = ta.macd(c, 12, 26, 9)
    if macd is not None:
        _macd_hist_col = next((col for col in macd.columns if "MACDH" in col.upper()), None)
        # BUG FIX (Finding N): previously had no else — if no column matched
        # (e.g. a pandas_ta version/naming change), macd_hist silently never
        # got created at all, unlike every other indicator's explicit None
        # fallback. Now consistent with the rest of this function.
        df["macd_hist"] = macd[_macd_hist_col] if _macd_hist_col is not None else None
    else:
        df["macd_hist"] = None

    bb = ta.bbands(c, 20, 2)
    if bb is not None:
        # BUG FIX (Finding M): next() with no default raises an uncaught
        # StopIteration on any column-naming mismatch, crashing the entire
        # calc_indicators() call (and by extension analyze() for that ticker)
        # with no graceful degradation — proven reproducible with a synthetic
        # naming mismatch. Now returns None instead of crashing, matching
        # every other indicator's failure-mode convention in this function.
        def _bb(kws):
            return next((col for col in bb.columns if any(k in col.upper() for k in kws)), None)
        _u, _l, _m = _bb(["BBU","UPPER"]), _bb(["BBL","LOWER"]), _bb(["BBM","MID"])
        df["bb_upper"] = bb[_u] if _u else None
        df["bb_lower"] = bb[_l] if _l else None
        df["bb_mid"]   = bb[_m] if _m else None
        df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]) if (_u and _l and _m) else None
    else:
        df["bb_upper"] = df["bb_lower"] = df["bb_mid"] = df["bb_width"] = None

    df["atr"] = ta.atr(h, l, c, 14)
    df["obv"] = ta.obv(c, v)

    adx_df   = ta.adx(h, l, c, 14)
    df["adx"] = adx_df["ADX_14"] if adx_df is not None and "ADX_14" in adx_df.columns else None

    mfi_s    = ta.mfi(h, l, c, v, 14)
    df["mfi"] = mfi_s if mfi_s is not None else None

    vol_ma20        = v.rolling(20).mean()
    df["vol_spike"] = v > (vol_ma20 * 1.5)
    # BUG FIX (audit finding, 2026-07-07): vol_p75 used to be ONE scalar computed
    # from the last 50 bars of whatever df was current at call time, then
    # broadcast as a fixed threshold across the ENTIRE column. That's correct
    # for the live/last row (its own trailing window) but non-causal for every
    # other row — each historical bar's vol_strong flag was silently compared
    # against a window anchored at today, not at that bar's own point in time.
    # No effect on a single live analyze() call (only `last` is read), but it
    # corrupts vol_strong for any historical/backtest consumer of this column
    # (backtest.py, or future mr_confirms calibration against this same log).
    # Now a proper rolling 50-bar trailing 75th percentile, causal at every bar.
    vol_p75_roll    = v.rolling(50, min_periods=20).quantile(0.75)
    df["vol_strong"]= v > vol_p75_roll

    prev_o, prev_c  = o.shift(1), c.shift(1)
    df["bull_engulf"]= ((prev_c < prev_o) & (c > o) & (o <= prev_c) & (c >= prev_o)).fillna(False)
    df["bear_engulf"]= ((prev_c > prev_o) & (c < o) & (o >= prev_c) & (c <= prev_o)).fillna(False)

    # P2-B: Donchian Channel (20-period)
    # dc_breakout_bull: close crossed above prior bar's upper channel (confirmed breakout)
    # dc_breakout_bear: close crossed below prior bar's lower channel
    df["dc_upper"]        = h.rolling(20).max()
    df["dc_lower"]        = l.rolling(20).min()
    df["dc_breakout_bull"] = c > df["dc_upper"].shift(1)
    df["dc_breakout_bear"] = c < df["dc_lower"].shift(1)

    # v7.2: AVWAP (Anchored VWAP) — capitulation/climax anchor, direction-aware.
    # BUG FIX vs the originally proposed design (audit finding, 2026-07-09):
    # anchoring purely to the highest-volume bar in a lookback does not
    # distinguish a capitulation BOTTOM from a blow-off TOP — a volume spike
    # happens at both, and the earlier proposal's code would have anchored
    # bullish at a euphoric top just as readily as at a real bottom. Fixed:
    # only anchor when that high-volume bar is ALSO a genuine local price
    # extreme — its low sits in the bottom 20% of the lookback's price range
    # (capitulation bottom -> bullish anchor) or its high sits in the top 20%
    # (blow-off top -> bearish anchor). Verified against synthetic bottom/top/
    # mid-range test cases before integration; mid-range volume spikes
    # (neither extreme) correctly produce no anchor rather than guessing.
    # Computed once from the tail (causal — uses only data up to "now", never
    # leaks forward), consumed only via last.get(), same convention as
    # dc_breakout_bull/bear and vol_strong.
    avwap_val, avwap_type = None, None
    _lookback = min(100, len(df))
    if _lookback >= 20:
        _recent = df.iloc[-_lookback:]
        _vmax_pos_in_recent = int(_recent["volume"].values.argmax())
        _anchor_pos = len(df) - _lookback + _vmax_pos_in_recent

        _window_low  = float(_recent["low"].min())
        _window_high = float(_recent["high"].max())
        _window_range = _window_high - _window_low

        if _window_range > 0:
            _bar_low  = float(df["low"].iloc[_anchor_pos])
            _bar_high = float(df["high"].iloc[_anchor_pos])
            _is_bottom_like = (_bar_low  - _window_low)  / _window_range <= 0.20
            _is_top_like    = (_window_high - _bar_high) / _window_range <= 0.20

            if _is_bottom_like and not _is_top_like:
                _seg = df.iloc[_anchor_pos:]
                _cv = _seg["volume"].sum()
                if _cv > 0:
                    avwap_val  = float((_seg["close"] * _seg["volume"]).sum() / _cv)
                    avwap_type = "bullish_capitulation"
            elif _is_top_like and not _is_bottom_like:
                _seg = df.iloc[_anchor_pos:]
                _cv = _seg["volume"].sum()
                if _cv > 0:
                    avwap_val  = float((_seg["close"] * _seg["volume"]).sum() / _cv)
                    avwap_type = "bearish_climax"
            # else: ambiguous (both or neither) -> no anchor, deliberately not a guess
    df["avwap_cap"]         = avwap_val
    df["avwap_anchor_type"] = avwap_type

    return df.dropna(subset=["close","volume"])


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 4 — STRUCTURE & REGIME
# ═══════════════════════════════════════════════════════════════════════════════
def find_structure(df, n=None, timeframe="1H"):
    # F7 — n now defaults to TF_STRUCTURE_N[timeframe] if not provided.
    # backtest.py calls find_structure(sl) — default "1H" keeps it backward-compatible.
    if n is None:
        n = TF_STRUCTURE_N.get(timeframe, 5)
    price = float(df["close"].iloc[-1])
    highs, lows = [], []
    high_indices, low_indices = [], []
    for i in range(n, len(df) - n):
        hi = df["high"].iloc[i]; lo = df["low"].iloc[i]
        if hi == df["high"].iloc[i-n:i+n+1].max():
            highs.append(float(hi)); high_indices.append(i)
        if lo == df["low"].iloc[i-n:i+n+1].min():
            lows.append(float(lo)); low_indices.append(i)

    def cluster_with_idx(vals, idxs, tol=0.005):
        """Cluster by price proximity, preserving one representative bar index per cluster.
        CORRECTION on Finding P: the original audit described this as comparing
        against a 'drifting last-kept value' vs a 'static anchor,' implying a
        runaway chaining bug. On inspection these are mathematically identical
        in this implementation (the anchor only ever updates when a new
        cluster starts, which is exactly when the last-kept value also
        changes) — verified: both formulations produce identical output on
        the original test case. No unbounded chaining actually occurs. The
        real, more modest property: adjacent cluster centers can be up to
        ~2x tol apart (a normal characteristic of greedy sequential 1D
        clustering), not the "everything collapses" failure originally
        feared. Not fixed because there was nothing to fix here — flagging
        this correction rather than leaving the earlier claim standing.
        """
        out_vals, out_idxs = [], []
        for v, ix in sorted(zip(vals, idxs)):   # sort by price value
            if not out_vals or abs(v - out_vals[-1]) / max(abs(out_vals[-1]), 1e-8) > tol:
                out_vals.append(v); out_idxs.append(ix)
        return out_vals, out_idxs

    highs, high_idx = cluster_with_idx(highs, high_indices)
    lows,  low_idx  = cluster_with_idx(lows,  low_indices)

    res_candidates = [h for h in highs if h > price]
    sup_candidates = [lo for lo in lows if lo < price]
    res = min(res_candidates) if res_candidates else float(df["high"].tail(20).max())
    sup = max(sup_candidates) if sup_candidates else float(df["low"].tail(20).min())
    # BUG FIX (Finding F): near_resistance/near_support used to fire off the
    # fallback value too — during a clean breakout to new highs (or breakdown
    # to new lows), no real pivot exists on that side, the fallback collapses
    # to ~current price, and the proximity check trivially became True. This
    # corrupted both scoring and mr_confirms (a crash with no real support
    # below could get a free mean-reversion "confirmation" from this alone).
    # Require a genuine pivot to exist before the proximity flag can fire.
    has_real_res = bool(res_candidates)
    has_real_sup = bool(sup_candidates)

    # ── P2-B: RSI divergence detection ──────────────────────────────────────
    # Compare the two most RECENT same-side pivot clusters by bar index (time order)
    # to detect classic price/RSI divergence.
    def _rsi_at(bar_idx):
        if "rsi" not in df.columns or bar_idx < 0 or bar_idx >= len(df):
            return None
        v = df["rsi"].iloc[bar_idx]
        return None if pd.isna(v) else float(v)

    rsi_div = None

    # Bearish divergence: newer resistance pivot is higher in price but RSI is lower
    res_by_time = sorted(
        [(v, ix) for v, ix in zip(highs, high_idx) if v > price],
        key=lambda x: x[1]          # sort ascending by bar index (oldest first)
    )
    if len(res_by_time) >= 2:
        (r_prior_val, r_prior_idx) = res_by_time[-2]
        (r_newer_val, r_newer_idx) = res_by_time[-1]
        r_prior_rsi = _rsi_at(r_prior_idx)
        r_newer_rsi = _rsi_at(r_newer_idx)
        if (r_prior_rsi is not None and r_newer_rsi is not None
                and r_newer_val > r_prior_val       # price: higher high
                and r_newer_rsi < r_prior_rsi):     # RSI: lower high
            rsi_div = "bearish"

    # Bullish divergence: newer support pivot is lower in price but RSI is higher
    if rsi_div is None:
        sup_by_time = sorted(
            [(v, ix) for v, ix in zip(lows, low_idx) if v < price],
            key=lambda x: x[1]
        )
        if len(sup_by_time) >= 2:
            (s_prior_val, s_prior_idx) = sup_by_time[-2]
            (s_newer_val, s_newer_idx) = sup_by_time[-1]
            s_prior_rsi = _rsi_at(s_prior_idx)
            s_newer_rsi = _rsi_at(s_newer_idx)
            if (s_prior_rsi is not None and s_newer_rsi is not None
                    and s_newer_val < s_prior_val       # price: lower low
                    and s_newer_rsi > s_prior_rsi):     # RSI: higher low
                rsi_div = "bullish"

    return {
        # P2-A Bug 13: raw floats — no round(x, 2) — so sub-cent assets don't collapse
        "resistance":      res,
        "support":         sup,
        "all_resistances": sorted([h for h in highs if h > price])[:3],
        "all_supports":    sorted([lo for lo in lows if lo < price], reverse=True)[:3],
        "near_resistance": has_real_res and price >= res * 0.985,
        "near_support":    has_real_sup and price <= sup * 1.015,
        "in_range":        sup < price < res,
        "range_pct":       round((res - sup) / sup * 100, 2) if sup > 0 else None,
        "rsi_divergence":  rsi_div,   # "bearish" | "bullish" | None
    }


def detect_regime(df, timeframe="1H"):
    # F6 — thresholds now scale per timeframe.
    thr = REGIME_THRESHOLDS.get(timeframe, REGIME_THRESHOLDS["1H"])
    p    = float(df["close"].iloc[-1])
    bw   = sf(df["bb_width"].iloc[-1])
    atr  = sf(df["atr"].iloc[-1])
    e9   = sf(df["ema9"].iloc[-1])
    e21  = sf(df["ema21"].iloc[-1])
    e9p  = sf(df["ema9"].iloc[-5]) if len(df) >= 5 else None
    atr_pct = atr / p * 100 if atr and p else None
    slope   = (e9 - e9p) / e9p * 100 if e9 and e9p else None
    if atr_pct and atr_pct > thr["atr_avoid"]:
        return "VOLATILE_AVOID", f"ATR {atr_pct:.1f}% — too hot"
    if bw and bw < thr["bb_squeeze"]:
        return "SQUEEZE", f"BB width <{thr['bb_squeeze']*100:.1f}%"
    if slope is not None and abs(slope) < thr["slope_flat"]:
        return "RANGING", "Flat EMA9"
    if slope and slope > thr["slope_trend"] and e9 and e21 and e9 > e21:
        return "TRENDING_UP",   f"EMA9 slope +{slope:.1f}%"
    if slope and slope < -thr["slope_trend"] and e9 and e21 and e9 < e21:
        return "TRENDING_DOWN", f"EMA9 slope {slope:.1f}%"
    return "NEUTRAL", "No clear regime"


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 5 — MTF ALIGNMENT
# ═══════════════════════════════════════════════════════════════════════════════
def _tf_bias(df):
    """Compute (bias, note, adx) from a df with ema9/ema21 already added."""
    e9  = sf(df["ema9"].iloc[-1])
    e21 = sf(df["ema21"].iloc[-1])
    pr  = sf(df["close"].iloc[-1])
    adx = sf(df["adx"].iloc[-1]) if "adx" in df.columns else None
    if not (e9 and e21 and pr):
        return "unknown", "no data", None
    if pr > e9 > e21:
        return "bullish", "Price>EMA9>EMA21", adx
    if pr < e9 < e21:
        return "bearish", "Price<EMA9<EMA21", adx
    return "neutral", "EMAs mixed", adx


def _prep_bias_df(df):
    """Add minimal indicators needed for TF bias computation."""
    d = df.copy()
    d["ema9"]  = ta.ema(d["close"], 9)
    d["ema21"] = ta.ema(d["close"], 21)
    adx_df = ta.adx(d["high"], d["low"], d["close"], 14)
    if adx_df is not None and "ADX_14" in adx_df.columns:
        d["adx"] = adx_df["ADX_14"]
    return d


def get_btc_context(ttl=300):
    # F9 — lock prevents duplicate BTC fetches when screener threads all expire at once
    if (time.time() - _BTC_CACHE["ts"]) < ttl:
        return _BTC_CACHE["bias"]
    with _BTC_LOCK:
        if (time.time() - _BTC_CACHE["ts"]) < ttl:
            return _BTC_CACHE["bias"]
        try:
            df_b, _ = fetch_ohlcv("BTC", "1H", 80)
            if df_b is not None and len(df_b) >= 30:
                d = _prep_bias_df(df_b)
                bias, _, _ = _tf_bias(d)
                _BTC_CACHE.update({"bias": bias, "ts": time.time()})
                return bias
        except Exception:
            pass
        # BUG FIX (2026-07-20, found while auditing analyze.py for the
        # Streamlit transition — new, not from any prior audit or session):
        # both failure paths above (exception, or a successful-but-
        # insufficient-data fetch) used to fall through to just refreshing
        # _BTC_CACHE["ts"] and returning whatever "bias" was already
        # cached — silently serving a POSSIBLY STALE bias as if it were a
        # fresh read for a full ttl (300s) more, with no way for any caller
        # to tell the difference. Compare fetch_rate() a few lines above,
        # which handles the identical "fetch failed" scenario correctly —
        # it sets an explicit fallback value AND labels it via src:
        # "fallback" so callers can distinguish live from fallback data.
        # This function had no equivalent label, so a single transient BTC
        # fetch hiccup could silently drag every non-BTC ticker's BTC-drag
        # scoring for 5 minutes on data indistinguishable from live.
        # Matching fetch_rate()'s honesty: mark it "unknown" rather than
        # freeze whatever was cached — the confidence-multiplier code
        # already has a defined, safe path for btc=="unknown" (0.85
        # multiplier), so this degrades gracefully rather than silently.
        _BTC_CACHE.update({"bias": "unknown", "ts": time.time()})
        return "unknown"


def get_mtf_alignment(ticker, base_tf, asset_type, trigger_df=None, force_refresh=False):
    """
    Fetch compass/engine/trigger TFs, compute alignment tier.
    trigger_df: already-fetched base TF df (avoids re-fetch).
    Returns dict with: layers, tier, verdict, btc_bias, htf_bias (compat).
    """
    c_tf, e_tf, t_tf = MTF_LAYERS.get(base_tf, ("1D", "4H", "1H"))
    fn = fetch_ohlcv if asset_type == "crypto" else fetch_stock
    layers = {}

    fetched_tf_map = {}   # actual data timeframe -> which layer label already has it
    for tf in dict.fromkeys([c_tf, e_tf, t_tf]):   # preserve order, dedupe
        fetch_tf = "1D" if (asset_type == "stock" and tf == "4H") else tf
        if tf == t_tf and trigger_df is not None:
            try:
                bias, note, adx = _tf_bias(_prep_bias_df(trigger_df))
                layers[tf] = {"bias":bias,"note":note,"adx":round(adx,1) if adx else None,"fetched_as":fetch_tf,
                              # DASHBOARD FIX (MTF sparklines): compact closing-price
                              # tail, native currency (sparkline shows SHAPE only, no
                              # axis labels, so no currency conversion needed here).
                              # This data was already fetched to compute bias/note/adx
                              # above — previously discarded once those 3 scalars were
                              # derived, same "computed and thrown away" pattern this
                              # file has fixed several times already.
                              "closes": [round(v, 8) for v in trigger_df["close"].tail(30).tolist()]}
                fetched_tf_map[fetch_tf] = tf
                continue
            except Exception:
                pass
        # BUG FIX (Finding H): for stocks, a 4H request silently becomes 1D
        # (no intraday 4H data from the free-tier source). For a 1H entry
        # timeframe, compass is ALSO 1D — so engine and compass used to fetch
        # and compute on identical data, making them agree by construction,
        # which inflated apparent "3 independent timeframes confirm" (FULL_BULL)
        # when only 2 genuinely independent readings existed. Reuse the
        # already-computed result instead, and mark it as collided so tier
        # classification can exclude it from the independent-confirmation count.
        if fetch_tf in fetched_tf_map:
            src_tf = fetched_tf_map[fetch_tf]
            layers[tf] = dict(layers[src_tf])
            layers[tf]["collided_with"] = src_tf
            continue
        try:
            if asset_type == "crypto":
                raw_result = fn(ticker, fetch_tf, 100, force_refresh=force_refresh)
            else:
                raw_result = fn(ticker, fetch_tf, 100)
            df_raw, err = raw_result[0], raw_result[1]
            if err or df_raw is None or len(df_raw) < 30:
                raise ValueError(err or "no data")
            bias, note, adx = _tf_bias(_prep_bias_df(df_raw))
            layers[tf] = {"bias":bias,"note":note,"adx":round(adx,1) if adx else None,"fetched_as":fetch_tf,
                          "closes": [round(v, 8) for v in df_raw["close"].tail(30).tolist()]}
            fetched_tf_map[fetch_tf] = tf
        except Exception:
            layers[tf] = {"bias":"unknown","note":"fetch failed","adx":None,"fetched_as":fetch_tf}

    c = layers.get(c_tf,{}).get("bias","unknown")
    e = layers.get(e_tf,{}).get("bias","unknown")
    t = layers.get(t_tf,{}).get("bias","unknown")

    # BUG FIX (Finding H continued): count only genuinely independent readings
    # toward the tier classification — a collided layer (identical underlying
    # data to another layer) doesn't add real confirmation.
    independent_vals, seen_src = [], set()
    for tf_label, val in [(c_tf,c),(e_tf,e),(t_tf,t)]:
        src = layers.get(tf_label,{}).get("collided_with") or tf_label
        if src not in seen_src:
            independent_vals.append(val); seen_src.add(src)
    bulls = independent_vals.count("bullish")
    bears = independent_vals.count("bearish")

    # BUG FIX (Finding O): the verdict text used to hardcode "mixed" (case 3)
    # or "neutral" (case 7) for the disagreeing timeframe regardless of what
    # it actually showed — including cases where it was genuinely bearish
    # (opposing, not merely mixed) or unknown (a fetch failure, not a
    # genuinely flat reading). Now reflects the real value.
    if   bulls == 3:                            tier,verdict = "FULL_BULL",    "All TFs bullish — best long setup"
    elif bears == 3:                            tier,verdict = "FULL_BEAR",    "All TFs bearish — NO LONG TRADES"
    elif c=="bullish" and t=="bullish":         tier,verdict = "PARTIAL_BULL", f"Compass+trigger bullish, {e_tf} {e} — valid, lower confidence"
    elif c=="bearish" and t=="bearish":         tier,verdict = "PARTIAL_BEAR", "Compass+trigger bearish — avoid longs"
    elif c=="bullish" and t=="bearish":         tier,verdict = "COUNTER_TREND",f"Pullback in {c_tf} uptrend — wait for {t_tf} reversal"
    elif c=="bearish" and t=="bullish":         tier,verdict = "COUNTER_TREND",f"Counter-trend bounce — {c_tf} bearish, risky long"
    elif e=="bullish" and t=="bullish":         tier,verdict = "PARTIAL_BULL", f"Engine+trigger bullish — valid setup, {c_tf} {c} (monitor)"
    elif e=="bearish" and t=="bearish":         tier,verdict = "PARTIAL_BEAR", f"Engine+trigger bearish — avoid longs, {c_tf} {c}"
    else:                                       tier,verdict = "MIXED",        "Mixed signals across timeframes — wait"

    btc_bias = "N/A"
    if asset_type == "crypto" and ticker != "BTC":
        btc_bias = get_btc_context()

    return {
        "layers":     layers,
        "tier":       tier,
        "verdict":    verdict,
        "btc_bias":   btc_bias,
        "compass_tf": c_tf,
        "engine_tf":  e_tf,
        "trigger_tf": t_tf,
        "htf_bias":   c,      # backward-compat: screener reads signal["htf_bias"]
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 6 — SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# v7.2 — NORMALIZATION HELPERS (multicollinearity fix)
# Converts heterogeneous raw indicator readings into comparable [-1,+1] units
# before they get combined, instead of the old flat "+1 per signal that
# fires" — which added apples, oranges, and wrenches on the assumption they
# were all worth the same one point regardless of how extreme the reading
# actually was, or how correlated it was with three other signals restating
# the same underlying fact.
# ═══════════════════════════════════════════════════════════════════════════════
def _norm_oscillator(value, lo=0.0, hi=100.0):
    """Bounded oscillator (RSI/Stochastic/MFI) -> [-1,+1], centered at its
    midpoint. +1 = maximally overbought, -1 = maximally oversold."""
    if value is None:
        return 0.0
    mid = (lo + hi) / 2.0
    span = (hi - lo) / 2.0
    return max(-1.0, min(1.0, (value - mid) / span))

def _norm_atr_distance(price, reference, atr, cap=2.0):
    """Distance of price from a reference level (EMA, VWAP, AVWAP), scaled
    by ATR so it's comparable across assets and volatility regimes instead
    of comparing raw price gaps, clipped to [-cap,+cap] then rescaled to
    [-1,+1]. Fails open (returns 0.0, no vote) on missing/invalid inputs."""
    if price is None or reference is None or not atr or atr <= 0:
        return 0.0
    d = (price - reference) / atr
    d = max(-cap, min(cap, d))
    return d / cap


def generate_signal(ticker, last, prev, structure, asset_type, regime, htf, btc,
                    news_list=None, sma200_above=None, timeframe="1H"):
    """
    htf = compass TF bias  ("bullish"|"bearish"|"neutral"|"unknown")
    btc = BTC macro bias   ("bullish"|"bearish"|"neutral"|"N/A")
    sma200_above = True if price > 1D SMA200, False if below, None if unknown.
    news_list kept as param for backtest.py backward-compat, ignored.
    timeframe needed for Bug 12 BB squeeze threshold lookup.

    v7.2 (2026-07-09): REGIME-NATIVE ROUTING + NORMALIZED SIGNAL GROUPS.
    Replaces the old flat additive walk (sc += 1 per signal, ~24 discrete
    lines, order-dependent) with four normalized [-1,+1] components --
    trend, momentum, volume, structure -- combined via regime-specific
    weights instead of one shared sequential accumulation.

    Why: audited and confirmed twice (independently, by two different
    review passes) that the old design double/triple-counted correlated
    signals as if independent (EMA cross + price>EMA50 + price>VWAP +
    Donchian breakout are all substantially restating "price is going up"
    computed four different ways), and that OBV's contribution depended on
    the RUNNING score's sign at that exact point in execution -- meaning
    the same underlying signals could score differently depending on
    execution order. Both are eliminated by construction here: correlated
    raw indicators are pre-averaged into one component before ever being
    weighted, and every component is computed from fixed inputs (never
    from a mutating running score).

    What is explicitly UNCHANGED, verbatim, from the prior version, because
    it was independently verified correct and is not what multicollinearity
    review was ever about: the SMA200 graduated-reduction gate, the ADX/HTF
    macro gate, mr_confirms, the HTF/BTC post-scoring overlays, the
    magnitude-based confidence formula, and the full return dict shape.
    """
    s, w, sc = [], [], 0
    g  = lambda d, k: sf(d.get(k))
    pr = g(last,"close")
    e9, e21, e50    = g(last,"ema9"), g(last,"ema21"), g(last,"ema50")
    pe9, pe21      = g(prev,"ema9"), g(prev,"ema21")
    rsi            = g(last,"rsi")
    stoch_k, stoch_d   = g(last,"stoch_k"),  g(last,"stoch_d")
    pstoch_k, pstoch_d = g(prev,"stoch_k"), g(prev,"stoch_d")
    mfi            = g(last,"mfi")
    bull_eng       = bool(last.get("bull_engulf", False))
    bear_eng       = bool(last.get("bear_engulf", False))
    bb_l, bb_u, bw = g(last,"bb_lower"), g(last,"bb_upper"), g(last,"bb_width")
    bb_mid         = g(last,"bb_mid")
    obv, pobv      = g(last,"obv"), g(prev,"obv")
    adx            = g(last,"adx")
    vwap           = g(last,"vwap")
    vol_spike      = bool(last.get("vol_spike",  False))
    atr            = g(last,"atr")
    # P2-B additions
    vol_str        = bool(last.get("vol_strong", False))
    dc_bull        = bool(last.get("dc_breakout_bull", False))
    dc_bear        = bool(last.get("dc_breakout_bear", False))
    rsi_div        = (structure.get("rsi_divergence") if structure else None)
    # v7.2 addition: direction-aware AVWAP anchor (see calc_indicators())
    avwap_cap      = g(last,"avwap_cap")
    avwap_type     = last.get("avwap_anchor_type")

    # ── MACRO GATE 1 — ADX/Regime/HTF strong-downtrend flag ─────────────────
    # UNCHANGED from prior version — see original Finding A/G comments.
    gate1_active = bool(adx and adx > 35 and regime == "TRENDING_DOWN" and htf == "bearish")
    if gate1_active:
        w.append(f"MACRO GATE: ADX {adx:.0f} + TRENDING_DOWN + {htf} HTF — trend-following longs blocked, mean-reversion longs allowed with confirmation")

    # ── MACRO GATE 2 — 1D SMA 200: strategy router, not a kill-switch ───────
    # UNCHANGED from prior version.
    below_sma200 = (sma200_above is False)
    if below_sma200:
        w.append("SMA200: price below 1D SMA 200 — trend-following longs blocked, mean-reversion longs allowed with confirmation")
    trend_blocked = below_sma200 or gate1_active

    # ═══════════════════════════════════════════════════════════════════════
    # NORMALIZED SIGNAL GROUPS (v7.2) — each component is a [-1,+1] read,
    # computed ONCE from fixed inputs, never from a mutating running score.
    # ═══════════════════════════════════════════════════════════════════════

    # ── TREND COMPONENT — collapses EMA50 position, VWAP position, AVWAP
    # position, and EMA9/21 cross into ONE reading. These four all
    # substantially restate "is price above its recent average" computed
    # four slightly different ways — averaging them (not summing) is what
    # actually fixes the multicollinearity, not just rescaling units.
    trend_votes = []
    if e50 and pr and atr:
        trend_votes.append(_norm_atr_distance(pr, e50, atr, cap=2.0))
    if vwap and pr and atr:
        trend_votes.append(_norm_atr_distance(pr, vwap, atr, cap=2.0))
    if avwap_cap and avwap_type and pr and atr:
        # Anchor TYPE already determines whether this vote is trustworthy —
        # calc_indicators() only sets avwap_cap when the anchor bar was a
        # genuine local extreme (bottom or top), never from volume alone.
        trend_votes.append(_norm_atr_distance(pr, avwap_cap, atr, cap=2.0))
    cross_vote = 0.0
    if e9 and e21 and pe9 and pe21:
        if   e9 > e21 and pe9 <= pe21: cross_vote = 1.0
        elif e9 < e21 and pe9 >= pe21: cross_vote = -1.0
        elif e9 > e21: cross_vote = 0.5
        elif e9 < e21: cross_vote = -0.5
    trend_votes.append(cross_vote)
    trend_component = sum(trend_votes) / len(trend_votes) if trend_votes else 0.0
    if trend_component > 0.15:   s.append(f"Trend context: bullish ({trend_component:+.2f})")
    elif trend_component < -0.15: s.append(f"Trend context: bearish ({trend_component:+.2f})")

    # ── MOMENTUM COMPONENT — collapses RSI, Stochastic, MFI, and BB-edge
    # touch into ONE contrarian reading. Oversold = positive/bullish vote.
    momentum_votes = []
    if rsi is not None:     momentum_votes.append(-_norm_oscillator(rsi))
    if stoch_k is not None: momentum_votes.append(-_norm_oscillator(stoch_k))
    if mfi is not None:     momentum_votes.append(-_norm_oscillator(mfi))
    if bb_l and bb_u and pr:
        if   pr <= bb_l: momentum_votes.append(1.0)
        elif pr >= bb_u: momentum_votes.append(-1.0)
    momentum_component = sum(momentum_votes) / len(momentum_votes) if momentum_votes else 0.0
    if rsi is not None: s.append(f"RSI {rsi:.1f}")

    # ── VOLUME COMPONENT — OBV direction checked against trend_component's
    # OWN lean (a fixed value computed above), NOT the running score. This
    # is the specific change that removes the order-dependence bug: OBV's
    # contribution can no longer depend on what earlier checks already did
    # to `sc`, because nothing here reads `sc` at all.
    volume_votes = []
    if obv is not None and pobv is not None:
        obv_dir = 1.0 if obv > pobv else (-1.0 if obv < pobv else 0.0)
        if trend_component > 0.1:
            if obv_dir > 0:   volume_votes.append(1.0);  s.append("OBV confirms buying pressure")
            elif obv_dir < 0: volume_votes.append(-0.5); w.append("OBV diverging — weak volume on rally")
        elif trend_component < -0.1:
            if obv_dir < 0:   volume_votes.append(-1.0); s.append("OBV confirms selling pressure")
            elif obv_dir > 0: volume_votes.append(0.5);  w.append("OBV/price divergence")
    volume_component = sum(volume_votes) / len(volume_votes) if volume_votes else 0.0

    # ── STRUCTURE COMPONENT — S/R proximity (tautology-fixed) + Donchian
    # breakout. vol_str remains a hard gate on breakout credit: no real
    # participation behind a breakout = no credit, same principle as before.
    is_tautological_overlap = (
        structure.get("near_resistance") and structure.get("near_support")
    ) if structure else False
    structure_votes = []
    if structure:
        if is_tautological_overlap:
            rp = structure.get("range_pct")
            rp_txt = f"{rp:.2f}%" if rp is not None else "<3%"
            s.append(f"Tight range ({rp_txt}) — consolidating between support "
                      f"and resistance, no directional edge")
        else:
            if structure.get("near_resistance"): structure_votes.append(-1.0); s.append("Near resistance")
            if structure.get("near_support"):    structure_votes.append(1.0);  s.append("Near support")
    if regime != "VOLATILE_AVOID":
        if dc_bull:
            if trend_blocked:
                w.append("Donchian upper breakout — blocked, trend-following long against macro downtrend")
            elif vol_str:
                structure_votes.append(1.0); s.append("Donchian upper breakout (vol confirmed)")
            else:
                w.append("Donchian upper breakout — vol_strong False, signal voided")
        if dc_bear:
            if vol_str:
                structure_votes.append(-1.0); w.append("Donchian lower breakout (bearish, vol confirmed)")
    # BB-squeeze bias only meaningful during an actual squeeze (structurally
    # "which side of the compressed range is price leaning").
    bb_squeeze_thr = REGIME_THRESHOLDS.get(timeframe, REGIME_THRESHOLDS["1H"])["bb_squeeze"]
    if bw and bw < bb_squeeze_thr and bb_mid and pr:
        if pr > bb_mid:
            structure_votes.append(1.0); s.append("BB Squeeze — price above mid (bullish bias)")
        else:
            structure_votes.append(-1.0); s.append("BB Squeeze — price below mid (bearish bias, wait for breakout)")
    structure_component = sum(structure_votes) / len(structure_votes) if structure_votes else 0.0

    # ═══════════════════════════════════════════════════════════════════════
    # REGIME ROUTER — weights decide which components matter per regime,
    # replacing the old end-of-pipeline reg_m rescale with actual per-regime
    # signal selection. TRENDING ignores oscillators (unreliable mid-trend,
    # matches the original RSI-in-downtrend carve-out). SQUEEZE leans on
    # volume+structure (a breakout needs real participation, EMAs are
    # tangled in a squeeze by definition). RANGING leans on momentum+
    # structure (mean-reversion IS the range-trading signal, trend is
    # meaningless in a range by definition). VOLATILE_AVOID dampens
    # everything near-uniformly, same spirit as the old 0.75x reduction.
    # ═══════════════════════════════════════════════════════════════════════
    regime_weights = {
        "TRENDING_UP":    {"trend": 5, "momentum": 0, "volume": 3, "structure": 2},
        "TRENDING_DOWN":  {"trend": 5, "momentum": 0, "volume": 3, "structure": 2},
        "SQUEEZE":        {"trend": 1, "momentum": 1, "volume": 4, "structure": 5},
        "RANGING":        {"trend": 0, "momentum": 5, "volume": 1, "structure": 4},
        "VOLATILE_AVOID": {"trend": 1, "momentum": 1, "volume": 1, "structure": 1},
    }.get(regime, {"trend": 3, "momentum": 2, "volume": 2, "structure": 2})  # NEUTRAL fallback: blended

    sc_float = (trend_component    * regime_weights["trend"]     +
                momentum_component * regime_weights["momentum"]  +
                volume_component   * regime_weights["volume"]    +
                structure_component * regime_weights["structure"])

    # ── PATTERN OVERRIDES — discrete, event-based signals (candle patterns,
    # divergences) don't naturally belong in a continuous normalized group;
    # kept as flat point adjustments on top, same as before. vol_str remains
    # a hard gate: a reversal candle with no real volume behind it is noise.
    near_sup = bool(structure.get("near_support"))    if structure else False
    near_res = bool(structure.get("near_resistance")) if structure else False
    if bull_eng:
        if vol_str: sc_float += (2 if near_sup else 1); s.append(f"Bullish engulfing (vol confirmed){' at support' if near_sup else ''}")
        else:       w.append("Bullish engulfing — vol_strong False, signal voided")
    if bear_eng:
        if vol_str: sc_float -= (2 if near_res else 1); s.append(f"Bearish engulfing (vol confirmed){' at resistance' if near_res else ''}")
        else:       w.append("Bearish engulfing — vol_strong False, signal voided")
    if mfi is not None:
        if   mfi < 20 and vol_str: sc_float += 1; s.append(f"MFI {mfi:.0f} oversold + vol confirmed (accumulation)")
        elif mfi > 80 and vol_str: sc_float -= 1; s.append(f"MFI {mfi:.0f} overbought + vol confirmed (distribution)")
    if regime in ("RANGING","SQUEEZE","NEUTRAL") and None not in (stoch_k,stoch_d,pstoch_k,pstoch_d):
        if   stoch_k > stoch_d and pstoch_k <= pstoch_d and stoch_k < 20:
            sc_float += 1; s.append(f"Stochastic bullish cross in oversold zone ({stoch_k:.0f})")
        elif stoch_k < stoch_d and pstoch_k >= pstoch_d and stoch_k > 80:
            sc_float -= 1; s.append(f"Stochastic bearish cross in overbought zone ({stoch_k:.0f})")

    sc = int(round(sc_float))

    # ── RSI DIVERGENCE SUPPRESSION — UNCHANGED from prior version.
    if rsi_div == "bearish" and sc > 0:
        old_sc = sc
        sc = max(0, sc - 2)
        w.append(f"RSI bearish divergence — momentum exhausted, score {old_sc}→{sc}")
    elif rsi_div == "bullish" and sc < 0:
        s.append("RSI bullish divergence — potential reversal signal")

    # ── VOLATILE_AVOID DAMPENING — UNCHANGED spirit from prior version.
    if regime == "VOLATILE_AVOID":
        sc = int(round(sc * 0.75)); w.append("High volatility — score reduced 25%")

    # ── HTF GATE — UNCHANGED from prior version (see original comments).
    if htf == "bearish" and sc > 0:
        if regime == "TRENDING_DOWN":
            old = sc; sc = int(sc * 0.5); w.append(f"HTF bearish + TRENDING_DOWN — heavy skepticism applied ({old}→{sc})")
        elif sc > 1:
            old = sc; sc = int(sc * 0.6); w.append(f"HTF bearish — score reduced ({old}→{sc})")
    elif htf == "bullish" and sc > 0:
        s.append("HTF aligned bullish (confidence boost only — Bug 11 fix)")
    elif htf == "bearish" and sc < 0:
        s.append("HTF aligned bearish (confidence boost only — Bug 11 fix)")

    # ── BTC CONTEXT — UNCHANGED from prior version.
    if asset_type == "crypto" and ticker != "BTC":
        if   btc == "bearish" and sc > 0: sc -= 2; w.append("BTC macro bearish drag")
        elif btc == "bullish" and sc > 0: s.append("BTC macro supportive")

    sc = int(round(sc))

    # ── BELOW-SMA200 COUNTER-TREND GATE — UNCHANGED from prior version
    # (graduated reduction, floor of 1, never a hard zero — this is the
    # fix that took this whole conversation to land; not touched here).
    mr_confirms = 0
    if structure and structure.get("near_support") and not structure.get("near_resistance"):
        mr_confirms += 1
    if rsi is not None and rsi < 30:                                             mr_confirms += 1
    if (None not in (stoch_k, stoch_d, pstoch_k, pstoch_d) and
            stoch_k > stoch_d and pstoch_k <= pstoch_d and stoch_k < 20):        mr_confirms += 1
    if bull_eng and vol_str:                                                     mr_confirms += 1
    if mfi is not None and mfi < 20 and vol_str:                                 mr_confirms += 1

    counter_trend_valid = False
    if trend_blocked and sc > 0:
        if mr_confirms >= 2:
            counter_trend_valid = True
            w.append(f"COUNTER-TREND BOUNCE: {mr_confirms} mean-reversion confirmations against macro downtrend — confidence ceiling is C (max), sizing should be reduced")
        else:
            old_sc = sc
            sc = max(1, round(sc * (0.5 if mr_confirms == 1 else 0.3)))
            w.append(f"Below 1D SMA200, only {mr_confirms}/2 mean-reversion confirmations — "
                     f"score reduced ({old_sc}→{sc}), not zeroed. Treat as reduced-conviction "
                     f"/ reduced-size, not a hard no-trade.")

    no_trade = (
        (regime == "VOLATILE_AVOID"  and abs(sc) < 3) or
        (regime == "RANGING"         and abs(sc) < 2) or
        (htf == "bearish"            and 0 < sc < 2)  or
        (regime == "TRENDING_DOWN"   and 0 < sc < 3)  or
        (trend_blocked and sc == 0)
    )
    if no_trade: w.append("NO_TRADE conditions met")

    if counter_trend_valid:
        strat = "COUNTER-TREND BOUNCE (below 1D SMA200)" if below_sma200 else "COUNTER-TREND BOUNCE (strong downtrend)"
    else:
        strat = (
            "STRONG BULLISH" if sc >= 5 else "BULLISH" if sc >= 3 else "MILD BULLISH" if sc >= 1 else
            "STRONG BEARISH" if sc <=-5 else "BEARISH" if sc <=-3 else "MILD BEARISH" if sc <=-1 else
            "FLAT — below 1D SMA200" if below_sma200 else "FLAT — strong downtrend" if gate1_active else "NEUTRAL"
        )
    inv = None
    if structure:
        inv = (
            "Close below support"    if sc > 0 else
            "Close above resistance" if sc < 0 else None
        )
    atr_v = sf(last.get("atr"))
    vol_label = (
        f"LOW ({atr_v/pr*100:.1f}% ATR)"    if atr_v and pr and atr_v/pr < 0.02 else
        f"MEDIUM ({atr_v/pr*100:.1f}% ATR)" if atr_v and pr and atr_v/pr < 0.05 else "HIGH"
    )

    # ── CONFIDENCE — UNCHANGED from prior version (magnitude-based fix).
    BASE = 14
    raw   = min(1.0, abs(sc) / BASE)
    reg_m = {"TRENDING_UP":1.15 if sc>0 else 0.85,"TRENDING_DOWN":0.85 if sc>0 else 1.1,
              "RANGING":0.75 if sc>2 else 1.0,"SQUEEZE":0.8,"VOLATILE_AVOID":0.5}.get(regime,1.0)
    htf_m = (0.65 if htf=="bearish" and sc>0 else 1.15 if htf=="bullish" and sc>0 else
             1.1  if htf=="bearish" and sc<0 else 0.65 if htf=="bullish" and sc<0 else
             0.85 if htf=="unknown" else 1.0)
    btc_m = 0.85 if btc=="unknown" and asset_type=="crypto" else 1.0
    conf  = min(100.0, raw * reg_m * htf_m * btc_m * 100)
    grade = "A" if conf >= 72 else "B" if conf >= 55 else "C" if conf >= 38 else "D"
    if counter_trend_valid and grade in ("A", "B"):
        grade = "C"
        conf  = min(conf, 55.0)
    if no_trade or sc == 0:
        conf, grade = 0.0, "N/A"

    return {
        "score":        sc,
        "bias":         "BULLISH" if sc>0 else "BEARISH" if sc<0 else "NEUTRAL",
        "no_trade":     no_trade,
        "tradeable":    not no_trade and abs(sc) >= 2,
        "counter_trend_valid": counter_trend_valid,
        "signals":      s,
        "warnings":     w,
        "strategy":     strat,
        "entry_style":  ("Counter-trend bounce — reduced size, tight stop" if counter_trend_valid else
                          "Breakout/pullback" if sc>=2 else "Wait for EMA21" if sc>=1 else
                          "No long" if sc<=-2 else "Wait reversal"),
        "invalidation": inv,
        "volatility":   vol_label,
        "regime":       regime,
        "htf_bias":     htf,
        "btc_bias":     btc,
        "confidence":   (f"{grade} — {'High' if grade=='A' else 'Moderate' if grade=='B' else 'Low' if grade=='C' else 'Very low'} conviction"
                          if grade != "N/A" else "N/A — no qualifying setup"),
        "components": {
            "trend": trend_component,
            "momentum": momentum_component,
            "volume": volume_component,
            "structure": structure_component
        }
    }



# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 7 — POSITION SIZING
# ═══════════════════════════════════════════════════════════════════════════════
def size_position(price_idr, atr_idr, asset_type, rate, btc_bias="unknown", score=0):
    if not atr_idr or atr_idr <= 0:
        return {"error":"ATR unavailable"}
    # BUG FIX (audit finding, 2026-07-29 — confirmed against real data from
    # an actual run, not a hypothetical: ETH score=-4/tradeable=true and
    # BBCA score=-9/tradeable=true both produced a full entry/SL/TP with SL
    # BELOW entry and TP ABOVE entry — the standard LONG structure — despite
    # being BEARISH, system-determined "avoid longs" signals. This function
    # never took score/direction as an input at all, so it unconditionally
    # built a long-only template regardless of bias, every time, for every
    # ticker. get_entry_zone() a few hundred lines below already treats
    # score<=-2 as NO_LONG ("Bearish bias — avoid longs", no aggressive/
    # conservative zone at all) — this was contradicting that same,
    # already-established convention two fields away in the identical
    # output dict. This codebase has no short-selling capability anywhere
    # else (get_entry_zone's NO_LONG branch, TIER_LABELS' "FULL_BEAR — NO
    # LONG TRADES") — counter_trend_valid, the one path that can produce a
    # LONG setup during a downtrend, is only ever True when sc > 0 (line
    # ~1436), so there is no legitimate case where a negative score should
    # still receive a real entry/SL/TP. The fix is therefore NOT to flip the
    # SL/TP direction into a short template — that would fabricate a trade
    # type this system was never built to execute — it's to stop generating
    # a real-looking long entry at all once the signal itself says avoid
    # longs, matching the existing "ATR unavailable" error-dict precedent
    # that pretty_print() already knows how to skip cleanly.
    if score <= -2:
        return {"error": "No long setup — bearish bias, avoid longs (system is long-only)"}
    cap       = CRYPTO_CAP if asset_type == "crypto" else STOCK_CAP
    max_loss  = cap * RISK_PCT
    max_alloc = cap * 0.10 if btc_bias == "bearish" else cap * 0.20
    sl_d      = max(atr_idr * 2.0, price_idr * 0.01)
    units     = max_loss / sl_d
    money     = units * price_idr
    capped    = money > max_alloc
    if capped:
        money = max_alloc; units = max_alloc / price_idr
    loss         = units * sl_d
    sl_price_idr = price_idr - sl_d
    return {
        "entry":      dual(price_idr, rate),
        "sl":         dual(sl_price_idr, rate),
        "sl_pct":     f"{sl_d/price_idr*100:.2f}%",
        "tp1":        dual(price_idr + sl_d,   rate),
        "tp2":        dual(price_idr + sl_d*2, rate),
        "tp2_pct":    f"{sl_d*2/price_idr*100:.2f}%",
        "units":      round(units,8) if asset_type=="crypto" else max(1,int(units/100)),
        "money":      dual(money, rate),
        "pct_pool":   f"{money/cap*100:.1f}%",
        "risk_idr":   dual(loss, rate),
        "warning": (
            # BUG FIX (Finding I): used to check btc_bias=="bearish" BEFORE
            # checking whether capping actually happened, so wide-stop setups
            # that never hit the cap still falsely claimed to be "Capped at
            # 10% pool" even at pct_pool well under that ceiling. Now checks
            # capped first, and picks the message based on which ceiling
            # actually bound.
            None if not capped else
            "Capped at 10% pool (BTC bearish macro)." if btc_bias=="bearish" else
            "Capped at 20% pool."
        ),
        # DASHBOARD FIX (chart TP/SL zones): dual() above only ever produces
        # a formatted display string ("Rp 1,234,567 ($68.90)") — nothing
        # could get the actual number back out without re-parsing text.
        # These raw IDR floats are the exact same values already computed
        # above, just not thrown away. analyze() converts these to the
        # chart's native currency right after calling this function — see
        # that comment for why the conversion happens there, not here.
        "entry_raw_idr": price_idr,
        "sl_raw_idr":    sl_price_idr,
        "tp1_raw_idr":   price_idr + sl_d,
        "tp2_raw_idr":   price_idr + sl_d * 2,
        # BUG FIX (audit finding, 2026-09): "money" and "risk_idr" above are
        # dual()-formatted DISPLAY STRINGS ("Rp 1,234,567 ($68.90)") — there
        # was no raw numeric field for the actual position size, unlike
        # entry/sl/tp1/tp2 which all already had a *_raw_idr counterpart.
        # This is what blocked a portfolio-level exposure cap from ever being
        # built: trade_ledger.py cannot sum "how much capital is committed
        # across open positions" from a formatted string. Same convention as
        # the four fields above — raw float, no rounding, caller formats for
        # display if needed.
        "money_raw_idr": money,
        "risk_raw_idr":  loss,
    }


def get_entry_zone(price_idr, atr_idr, structure, score, rate, is_global, counter_trend=False):
    if not atr_idr:
        return {"error":"ATR unavailable"}
    sup_n = structure.get("support");    res_n = structure.get("resistance")
    sup   = sup_n * rate if (is_global and sup_n) else sup_n
    res   = res_n * rate if (is_global and res_n) else res_n
    # THRESHOLD FIX: aligned to generate_signal()'s own "tradeable" definition
    # (abs(score) >= 2) instead of a disconnected >=3/<=-3 that silently hid
    # real setups in the 2-point score band — the exact "no clarity" gap found
    # during the Trade Setup sanity check.
    if score >= 2:
        return {
            "bias":        "LONG",
            "aggressive":  {"low":dual(price_idr-0.25*atr_idr,rate),"high":dual(price_idr+0.25*atr_idr,rate),"note":"Market / limit near price"},
            "conservative":{"low":dual(price_idr-0.75*atr_idr,rate),"high":dual(price_idr-0.25*atr_idr,rate),"note":"Pullback to EMA9"},
            # BUG FIX (Finding D): a counter-trend bounce's whole thesis is
            # "buy near support now" — showing a "wait for breakout above
            # resistance" note alongside it was a contradictory second thesis
            # on the same setup. Suppressed for counter-trend bounces.
            "breakout":    ({"level":dual(res*1.001,rate),"note":"Confirmed close above resistance"} if (res and not counter_trend) else None),
        }
    if score <= -2:
        return {"bias":"NO_LONG","note":"Bearish bias — avoid longs",
                "watch":{"level":dual(sup,rate),"note":"Wait for reversal confirmation"}}
    return {"bias":"RANGE",
            "low": dual(sup-0.3*atr_idr if sup else price_idr-atr_idr,   rate),
            "high":dual(sup+0.5*atr_idr if sup else price_idr-0.5*atr_idr,rate),
            "note":"No strong edge — range trade or wait for breakout"}


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 8 — PRETTY PRINT
# ═══════════════════════════════════════════════════════════════════════════════
TIER_LABELS = {
    "FULL_BULL":    "▲▲ FULL BULLISH    — best long setup",
    "PARTIAL_BULL": "▲· PARTIAL BULL    — valid, lower confidence",
    "MIXED":        "·· MIXED           — wait for clarity",
    "COUNTER_TREND":"⚡ COUNTER-TREND   — risky, high-skill only",
    "PARTIAL_BEAR": "▼· PARTIAL BEAR    — avoid longs",
    "FULL_BEAR":    "▼▼ FULL BEARISH    — NO LONG TRADES",
    "UNKNOWN":      "?? UNKNOWN         — insufficient data",
}

def _ln(txt=""):
    inner = W - 4
    line  = str(txt)
    if len(line) > inner:
        line = line[:inner-1] + "…"
    return f"║  {line:<{inner}}║"

def _sep(): return "╠" + "═"*(W-2) + "╣"
def _div(): return "╟" + "─"*(W-2) + "╢"
def _top(): return "╔" + "═"*(W-2) + "╗"
def _bot(): return "╚" + "═"*(W-2) + "╝"


def pretty_print(r: dict):
    from datetime import datetime, timezone
    now  = datetime.now(timezone.utc).strftime("%a %d %b  %H:%M UTC")
    sig  = r.get("signal",      {})
    mtf  = r.get("mtf",         {})
    ind  = r.get("indicators",  {})
    st   = r.get("structure",   {})
    tr   = r.get("trade_setup", {})
    vol  = r.get("volume",      {})
    ez   = r.get("entry_zone",  {})
    pr   = r.get("price_idr_raw", 0) or 0

    # P2-A Bug 13: precision-aware price display helpers
    rate_raw  = r.get("rate_raw")
    is_global = r.get("is_global", r.get("asset_type") == "crypto")

    def _pd_compact(v):
        """Compact IDR price string for tight table cells — no Rp prefix."""
        if v is None: return "?"
        fv = float(v)
        idr = fv * rate_raw if (is_global and rate_raw) else fv
        if idr >= 1_000_000_000: return f"{idr/1_000_000_000:.3g}B"
        if idr >= 1_000_000:     return f"{idr/1_000_000:.3g}M"
        if idr >= 1_000:         return f"{int(idr):,}"
        if idr >= 1:             return f"{idr:.4g}"
        return f"{idr:.4g}"

    def _pdu(v):
        """Full dual Rp+USD display for structure levels."""
        if v is None: return "?"
        fv = float(v)
        if is_global and rate_raw:
            return dual(fv * rate_raw, rate_raw)
        elif rate_raw:
            return dual(fv, rate_raw)
        return dual(fv)

    score  = sig.get("score", 0)
    no_t   = sig.get("no_trade", False)
    tier   = mtf.get("tier", "UNKNOWN")
    c_tf   = mtf.get("compass_tf", "1D")
    e_tf   = mtf.get("engine_tf",  "4H")
    t_tf   = mtf.get("trigger_tf", "1H")
    layers = mtf.get("layers", {})

    out = []
    out.append(_top())
    usd_hdr = r.get('price_usd') or ''
    out.append(_ln(f"{r['ticker']}  │  {r['timeframe']}  │  {usd_hdr}" if usd_hdr else f"{r['ticker']}  │  {r['timeframe']}"))
    out.append(_ln(f"{r.get('price_idr','')}"))
    out.append(_ln(f"{now}  │  {r.get('source','')}"))
    out.append(_sep())

    # MTF
    out.append(_ln("MULTI-TIMEFRAME TREND"))
    out.append(_div())
    shown = []
    # BUG FIX (Finding E): _tf_bias() (this display) and detect_regime() (used
    # for scoring, shown separately in the Signal section) are independently
    # thresholded and can genuinely disagree on identical data — proven: price
    # can sit above both EMAs (bias=bullish) while the EMA9 slope is flat
    # (regime=RANGING). Neither is wrong, but showing them with no cross-
    # reference reads as a contradiction. Flag it inline when it happens.
    _entry_regime = sig.get("regime")
    for tf_key, role in [(c_tf,"compass"),(e_tf,"engine"),(t_tf,"entry")]:
        if tf_key in shown: continue
        shown.append(tf_key)
        lyr   = layers.get(tf_key, {})
        bias  = lyr.get("bias","unknown")
        note  = lyr.get("note","")
        adxv  = lyr.get("adx")
        arrow = "▲" if bias=="bullish" else ("▼" if bias=="bearish" else "→")
        here  = " ← entry TF" if tf_key == t_tf else ""
        adxs  = f"  ADX {adxv:.0f}" if adxv else ""
        regime_note = ""
        if tf_key == t_tf and _entry_regime:
            conflicts = (
                (bias == "bullish" and _entry_regime in ("RANGING","TRENDING_DOWN","VOLATILE_AVOID")) or
                (bias == "bearish" and _entry_regime in ("RANGING","TRENDING_UP"))
            )
            if conflicts:
                regime_note = f"  (regime: {_entry_regime} — different measure, see Signal)"
        out.append(_ln(f"  {tf_key:<4}  {arrow} {bias.upper():<8}  {note}{adxs}{here}{regime_note}"))

    btc_b = mtf.get("btc_bias","N/A")
    if btc_b != "N/A":
        ba = "▲" if btc_b=="bullish" else ("▼" if btc_b=="bearish" else "→")
        out.append(_ln(f"  BTC   {ba} {btc_b.upper()}  (market context)"))

    out.append(_ln())
    out.append(_ln(f"  {TIER_LABELS.get(tier, tier)}"))
    out.append(_ln(f"  {mtf.get('verdict','')}"))
    if tier in ("PARTIAL_BULL", "PARTIAL_BEAR") and sig.get("score", 0) >= 3:
        score_here = sig.get("score", 0)
        out.append(_ln(f"  ↳ Entry TF score +{score_here} — engine+trigger aligned, {c_tf} compass neutral"))
    out.append(_sep())

    # Indicators
    out.append(_ln("INDICATORS  (entry timeframe)"))
    out.append(_div())

    # P2-A Bug 13: e9v/e21v/e50v are now raw floats; use is not None + _pd_compact()
    e9v = ind.get("ema9"); e21v = ind.get("ema21"); e50v = ind.get("ema50")
    # BUG FIX (audit finding, 2026-07-08 — "gaslighting display bug"): e9v/e21v/e50v
    # are native-currency (USD for Binance/is_global tickers), but pr here is
    # price_idr_raw (IDR). IDR values run ~17,900x larger than the equivalent USD
    # figure for the same asset, so `pr > e9v` was mathematically guaranteed true
    # for every is_global ticker regardless of real trend — making the FULL BEAR
    # and Below EMA9 branches dead code. generate_signal() itself was never
    # affected (it uses its own separately-scoped, unit-consistent pr from the
    # same native-currency `last` dict) — this was display-only, but confirmed
    # across every VWAP line in every test output shared this session: 14/14
    # printed "above", 0/14 ever printed "below", despite the Signals section
    # showing "Price below VWAP" 7 times in those same runs. Converting to IDR
    # here before comparing, same conversion _pd_compact()/_pdu() already apply
    # for the display TEXT — now the branch selection matches it too.
    e9v_cmp  = e9v  * rate_raw if (is_global and rate_raw and e9v  is not None) else e9v
    e21v_cmp = e21v * rate_raw if (is_global and rate_raw and e21v is not None) else e21v
    e50v_cmp = e50v * rate_raw if (is_global and rate_raw and e50v is not None) else e50v
    if e9v_cmp is not None and e21v_cmp is not None and e50v_cmp is not None and pr:
        if   pr > e9v_cmp > e21v_cmp > e50v_cmp: ema_s = f"▲ FULL BULL  9:{_pd_compact(e9v)}  21:{_pd_compact(e21v)}  50:{_pd_compact(e50v)}"
        elif pr < e9v_cmp < e21v_cmp < e50v_cmp: ema_s = f"▼ FULL BEAR  9:{_pd_compact(e9v)}  21:{_pd_compact(e21v)}  50:{_pd_compact(e50v)}"
        elif pr > e9v_cmp:                       ema_s = f"→ Above EMA9  {_pd_compact(e9v)}  (21:{_pd_compact(e21v)})"
        else:                                    ema_s = f"→ Below EMA9  {_pd_compact(e9v)}  (21:{_pd_compact(e21v)})"
    else:
        ema_s = "N/A"
    out.append(_ln(f"  EMA stack:  {ema_s}"))

    rsi = ind.get("rsi")
    if rsi is not None:
        rx = " ← OVERSOLD" if rsi<30 else (" ← OVERBOUGHT" if rsi>70 else "")
        rd = "▲" if rsi>55 else ("▼" if rsi<45 else "→")
        out.append(_ln(f"  RSI 14:     {rsi:.1f}  {rd}{rx}"))

    sk = ind.get("stoch_k"); sd2 = ind.get("stoch_d")
    if sk is not None:
        sx = " OVERSOLD" if sk<20 else (" OVERBOUGHT" if sk>80 else "")
        out.append(_ln(f"  Stoch K/D:  {sk:.0f}/{sd2:.0f}{sx}"))

    # P2-A Bug 13: MACD hist is a raw float now; use general format (not :.2f)
    mhv = ind.get("macd_hist")
    if mhv is not None:
        out.append(_ln(f"  MACD hist:  {mhv:.6g}  {'▲' if mhv>0 else '▼'}"))

    adxv = ind.get("adx")
    if adxv is not None:
        an = "strong trend" if adxv>35 else ("trending" if adxv>25 else "weak/ranging")
        out.append(_ln(f"  ADX:        {adxv:.1f}  ({an})"))

    atp = r.get("atr_pct")
    if atp is not None:
        an2 = "low" if atp<1 else ("normal" if atp<3 else "HIGH")
        out.append(_ln(f"  ATR%:       {atp:.2f}%  ({an2} volatility)"))

    vr = vol.get("ratio")
    if vr is not None:
        vn = "▲ high" if vr>=1.5 else ("→ normal" if vr>=0.8 else "▼ low")
        out.append(_ln(f"  Vol ratio:  {vr:.2f}×  {vn}"))

    # P2-A Bug 13: VWAP is raw float; use _pdu() for sub-cent safe display
    vwv = ind.get("vwap")
    if vwv is not None and pr:
        # BUG FIX (audit finding, 2026-07-08 — same "gaslighting display bug" as
        # the EMA stack above): vwv is native-currency, pr is IDR. Convert before
        # comparing so this matches what generate_signal() actually scored.
        vwv_cmp = vwv * rate_raw if (is_global and rate_raw and vwv is not None) else vwv
        vwn = "above ▲" if pr > vwv_cmp else "below ▼"
        out.append(_ln(f"  VWAP(20):   {_pdu(vwv)}  price {vwn}"))

    bwv = ind.get("bb_width")
    if bwv is not None:
        # Bug 12: BB squeeze threshold from REGIME_THRESHOLDS, not hardcoded 0.025
        bb_thr = REGIME_THRESHOLDS.get(r.get("actual_tf","1H"), REGIME_THRESHOLDS["1H"])["bb_squeeze"]
        sq = "  ⚡ SQUEEZE" if bwv < bb_thr else ""
        out.append(_ln(f"  BB width:   {bwv:.4f}{sq}"))

    mfiv = ind.get("mfi")
    if mfiv is not None:
        mn = "oversold" if mfiv<20 else ("overbought" if mfiv>80 else "neutral")
        out.append(_ln(f"  MFI:        {mfiv:.1f}  ({mn})"))

    # P2-B: Donchian display
    dc_u = ind.get("dc_upper"); dc_l = ind.get("dc_lower")
    dc_bull_b = ind.get("dc_breakout_bull", False)
    dc_bear_b = ind.get("dc_breakout_bear", False)
    if dc_u is not None and dc_l is not None:
        dc_flag = "  ⚡ BULL BREAK" if dc_bull_b else ("  ⚡ BEAR BREAK" if dc_bear_b else "")
        out.append(_ln(f"  Donchian:   U:{_pd_compact(dc_u)}  L:{_pd_compact(dc_l)}{dc_flag}"))

    # P2-B: SMA200 display
    sma200_above = r.get("sma200_above")
    if sma200_above is not None:
        sma_s = "▲ Above 1D SMA 200" if sma200_above else "▼ BELOW 1D SMA 200  ← gate active"
        out.append(_ln(f"  SMA200:     {sma_s}"))

    out.append(_sep())

    # Structure
    out.append(_ln("STRUCTURE"))
    out.append(_div())
    # P2-A Bug 13: resistance_usd / support_usd now dual-formatted (set in analyze())
    # Fallback to _pdu() only if somehow missing
    res_s = st.get("resistance_usd") or _pdu(st.get("resistance")) or "?"
    sup_s = st.get("support_usd")    or _pdu(st.get("support"))    or "?"
    out.append(_ln(f"  Resistance: {res_s}{'  ⚠ NEAR' if st.get('near_resistance') else ''}"))
    out.append(_ln(f"  Support:    {sup_s}{'  ← watch for bounce' if st.get('near_support') else ''}"))
    rp = st.get("range_pct")
    if rp: out.append(_ln(f"  Range:      {rp:.2f}%  ({'tight' if rp<3 else 'normal' if rp<8 else 'wide'})"))
    ar = st.get("all_resistances",[]); as_ = st.get("all_supports",[])
    # P2-A Bug 13: use _pd_compact() for compact All R/S display
    if ar:  out.append(_ln(f"  All R:      {', '.join(_pd_compact(x) for x in ar)}"))
    if as_: out.append(_ln(f"  All S:      {', '.join(_pd_compact(x) for x in as_)}"))
    # P2-B: RSI divergence in structure section
    rsi_div_val = st.get("rsi_divergence")
    if rsi_div_val:
        div_arrow = "▼" if rsi_div_val == "bearish" else "▲"
        out.append(_ln(f"  RSI Div:    {div_arrow} {rsi_div_val.upper()} divergence detected"))
    out.append(_sep())

    # Signal
    out.append(_ln("SIGNAL"))
    out.append(_div())
    bar  = ("█" if score>0 else "░") * min(abs(score),12) + "·" * (12-min(abs(score),12))
    sdir = "+" if score>0 else ""
    out.append(_ln(f"  Score:      {sdir}{score}  [{bar}]"))
    out.append(_ln(f"  Strategy:   {sig.get('strategy','')}"))
    out.append(_ln(f"  Confidence: {sig.get('confidence','')}"))
    out.append(_ln(f"  Regime:     {r.get('regime','?')}  ({r.get('regime_note','')})"))
    if no_t: out.append(_ln()); out.append(_ln("  ⚠  NO TRADE — conditions not met"))
    inv = sig.get("invalidation")
    if inv:
        if "resistance" in inv and st.get("resistance_usd"):
            inv = f"{inv} ({st['resistance_usd']})"
        elif "support" in inv and st.get("support_usd"):
            inv = f"{inv} ({st['support_usd']})"
        out.append(_ln(f"  Invalidation: {inv}"))
    sigs = sig.get("signals",[])
    if sigs:
        out.append(_ln()); out.append(_ln("  Signals:"))
        for x in sigs[:8]: out.append(_ln(f"    + {x}"))
    wrns = sig.get("warnings",[])
    if wrns:
        out.append(_ln()); out.append(_ln("  Warnings:"))
        for x in wrns: out.append(_ln(f"    ! {x}"))
    out.append(_sep())

    # Trade setup
    out.append(_ln("TRADE SETUP"))
    out.append(_div())
    bias_lbl    = ez.get("bias","?")
    entry_style = sig.get("entry_style", "")   # Bug 10 fix
    tradeable = sig.get("tradeable", False)
    if tr and not tr.get("error") and tradeable:
        out.append(_ln(f"  Direction:  {bias_lbl}"))
        if entry_style:
            out.append(_ln(f"  Style:      {entry_style}"))
        out.append(_ln(f"  Entry:      {tr.get('entry','?')}"))
        out.append(_ln(f"  SL:         {tr.get('sl','?')}  ({tr.get('sl_pct','?')})  2×ATR"))
        out.append(_ln(f"  TP1:        {tr.get('tp1','?')}  1:1 R:R"))
        out.append(_ln(f"  TP2:        {tr.get('tp2','?')}  {tr.get('tp2_pct','')}  1:2 R:R"))
        agg = ez.get("aggressive") or {}
        con = ez.get("conservative") or {}
        brk = ez.get("breakout")
        if agg or con or brk:
            out.append(_ln())
            out.append(_ln("  Entry Zones:"))
            if agg:
                out.append(_ln(f"    Aggressive:   {agg.get('low','?')} – {agg.get('high','?')}"))
                out.append(_ln(f"                  ({agg.get('note','')})"))
            if con:
                out.append(_ln(f"    Conservative: {con.get('low','?')} – {con.get('high','?')}"))
                out.append(_ln(f"                  ({con.get('note','')})"))
            if brk:
                out.append(_ln(f"    Breakout:     {brk.get('level','?')}"))
                out.append(_ln(f"                  ({brk.get('note','')})"))
        elif ez.get("bias") == "RANGE":
            out.append(_ln())
            out.append(_ln(f"  Range zone:  {ez.get('low','?')} – {ez.get('high','?')}"))
            out.append(_ln(f"               ({ez.get('note','')})"))
        out.append(_ln())
        out.append(_ln(f"  Risk:       {tr.get('risk_idr','?')}  per trade"))
        out.append(_ln(f"  Position:   {tr.get('money','?')}  ({tr.get('pct_pool','?')} of pool)"))
        # P2-A Bug 14: localized units display
        units_raw = tr.get('units', '?')
        if isinstance(units_raw, float):
            units_s = f"{units_raw:,.8f}".rstrip('0').rstrip('.')
        elif isinstance(units_raw, int):
            units_s = f"{units_raw:,}"
        else:
            units_s = str(units_raw)
        out.append(_ln(f"  Units:      {units_s}"))
        if tr.get("warning"): out.append(_ln(f"  ⚠  {tr['warning']}"))
    else:
        out.append(_ln(f"  Direction:  {bias_lbl}"))
        if entry_style:
            out.append(_ln(f"  Style:      {entry_style}"))
        if ez.get("bias") == "NO_LONG":
            # BUG FIX: watch level was already computed by get_entry_zone() but
            # this branch had no case for it, so it silently never displayed.
            watch = ez.get("watch") or {}
            out.append(_ln("  No trade — bearish signal, do not long"))
            if watch.get("level"):
                out.append(_ln(f"  Watch:      {watch['level']}  ({watch.get('note','')})"))
        elif no_t:        out.append(_ln("  No trade — conditions not met"))
        else:             out.append(_ln("  Signal too weak — wait for stronger setup"))
        if ez.get("bias") == "RANGE":
            out.append(_ln(f"  Range zone: {ez.get('low','?')} – {ez.get('high','?')}"))
            out.append(_ln(f"              ({ez.get('note','')})"))
    out.append(_bot())
    print("\n".join(out))


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 9 — analyze() — main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════
def analyze(ticker, timeframe="1H", asset_type=None, skip_mtf=False, force_refresh=False,
            log_signal=True):
    """
    Main entry point. Returns result dict.
    Backward-compatible with screener.py (reads signal["htf_bias"], signal["score"], etc.)
    and backtest.py (imports fetch_ohlcv, fetch_stock, calc_indicators, find_structure,
    detect_regime, generate_signal, CRYPTO_TICKERS, sf).

    log_signal=True (default) → appends this call's result to madbot_signals.csv,
                      exactly as before this parameter existed. Set False for any
                      caller that shouldn't pollute the calibration dataset — e.g.
                      an interactive Streamlit dashboard calling analyze() on every
                      widget rerun/hover. CLI and screener.py never pass this, so
                      their behavior is byte-for-byte unchanged.
    skip_mtf=True  → skips 3-TF fetch; screener uses this for batch speed.
                      SMA200 gate still runs (one 1D fetch, uses ccxt singleton).
    force_refresh=True → bypasses the OHLCV disk cache for every fetch this
                      call makes (entry TF, SMA200 1D, and — if skip_mtf is
                      False — the MTF compass/engine fetches too). Added
                      2026-07-19: this parameter didn't exist before, so
                      screener.py's --no-cache flag had no way to reach this
                      file at all — it only ever busted screener.py's own
                      scan-level cache, while analyze()'s own candle data
                      could silently still be up to the OHLCV cache's TTL old
                      (600s/1H, 1800s/4H, 14400s/1D) on a "fresh" run.
    """
    ticker     = ticker.upper()
    timeframe  = TF_NORM.get(timeframe.lower(), timeframe)
    asset_type = asset_type or _detect_asset_type(ticker)
    fn         = fetch_ohlcv if asset_type == "crypto" else fetch_stock

    # Bug 7 fix: fetch_stock() returns (df, err, actual_tf) for 4H→1D substitution
    if asset_type == "crypto":
        df, err = fn(ticker, timeframe, force_refresh=force_refresh)
        actual_tf = timeframe
    else:
        # fetch_stock() has no disk cache layer of its own (Finnhub/yfinance
        # calls only) — nothing for force_refresh to bypass here.
        df, err, actual_tf = fn(ticker, timeframe)

    if err:
        return {"error":err, "ticker":ticker, "timeframe":timeframe}

    # DASHBOARD FIX (Step 2): sanity-check the raw fetched frame before any
    # indicator is computed from it. Fail-open — a bad bar doesn't stop
    # analyze(), it just stops being INVISIBLE (see validate_ohlcv() above).
    data_quality_warnings = validate_ohlcv(df, ticker, timeframe)

    rate, rate_src = fetch_rate()
    is_global      = df.attrs.get("source") == "binance"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = calc_indicators(df)

    if df.empty or len(df) < 50:
        return {"error":"Not enough data after indicator warm-up (need 50+ bars)", "ticker":ticker}

    price_native_ohlcv = float(df["close"].iloc[-1])
    atr_native         = sf(df["atr"].iloc[-1])

    live_tag   = "ohlcv close"
    live_usd   = None
    if asset_type == "crypto":
        _live = fetch_live_price(ticker)
        if _live:
            live_usd  = _live
            live_tag  = "live ticker"

    # BUG FIX (Finding L): scoring/structure/regime are all computed against
    # price_native_ohlcv (the last CLOSED candle), but the displayed price and
    # actual entry/SL/TP figures use live_usd when available — a real,
    # previously undocumented divergence point. In a fast-moving market these
    # can drift apart enough to matter (e.g. "near support" was true at the
    # closed-candle price but live price has already moved past it). Surface
    # it explicitly instead of leaving it silent.
    # BUG FIX (audit finding, 2026-09, confirmed real via a live scan log
    # where EVERY successfully-fetched ticker showed exactly "100.0%"
    # divergence — a uniform, non-varying value across different tickers
    # and price levels is the signature of a unit-mismatch bug, not real
    # market noise, which would vary per ticker): fetch_live_price() always
    # queries Binance's USDT ticker directly via a raw requests.get() call
    # (see that function above) — it does NOT go through _resilient_call,
    # so it is completely unaffected by the "binance" circuit breaker state.
    # Meanwhile price_native_ohlcv comes from whichever source fetch_ohlcv()
    # actually returned — Binance (USD-denominated, is_global=True) OR the
    # Indodax fallback (IDR-denominated, is_global=False). When the OHLCV
    # fetch fell back to Indodax (e.g. because the "binance" circuit was
    # open from the bad-symbol issue fixed above) while the independent
    # live-ticker call kept succeeding, this comparison was directly
    # subtracting an IDR value (~16,000x larger) from a USD value with no
    # currency normalization — mathematically guaranteed to land near 100%
    # regardless of the real price relationship. Normalize to the same
    # currency (USD) before comparing, mirroring the same is_global check
    # already used a few lines below for price_idr/price_usd_val.
    price_divergence_warning = None
    if live_usd and price_native_ohlcv:
        ohlcv_usd = price_native_ohlcv if is_global else price_native_ohlcv / rate
        _drift_pct = abs(live_usd - ohlcv_usd) / ohlcv_usd * 100
        if _drift_pct >= 1.0:
            price_divergence_warning = (
                f"Live price has moved {_drift_pct:.1f}% from the last closed candle "
                f"used for scoring — signals/structure reflect the older price"
            )

    if is_global:
        price_usd_val = live_usd if live_usd else price_native_ohlcv
        price_idr     = price_usd_val * rate
    elif asset_type == "crypto":
        if live_usd:
            price_idr     = live_usd * rate
            price_usd_val = live_usd
        else:
            price_idr     = price_native_ohlcv
            price_usd_val = price_idr / rate
    else:
        price_idr     = price_native_ohlcv
        price_usd_val = None

    atr_idr      = atr_native * rate if is_global else atr_native
    atr_pct_val  = (atr_idr / price_idr * 100) if (atr_idr and price_idr) else None

    # F7 — pass actual_tf so pivot lookback uses correct scale (Bug 7 fix)
    structure = find_structure(df, timeframe=actual_tf)
    last      = df.iloc[-1].to_dict()
    prev      = df.iloc[-2].to_dict()
    vol_avg   = float(df["volume"].tail(20).mean())
    last["volume_ratio"] = last["volume"] / vol_avg if vol_avg > 0 else 1.0

    # F6 — pass actual_tf so ATR/slope thresholds use correct TF values (Bug 7 fix)
    regime, rnote = detect_regime(df, timeframe=actual_tf)

    # ── P2-B: SMA200 Macro Filter ────────────────────────────────────────────
    # Fetches 1D data with 210-bar limit (SMA200 needs 200 bars of warmup).
    # Uses ccxt singleton — no new TLS handshake.
    # If the entry timeframe IS 1D and df already has 200+ bars, reuse it directly.
    # For skip_mtf=True (screener): this still runs — it is one lightweight 1D call,
    # far less than the 3 MTF calls skip_mtf avoids.
    # Phase 3 note: data_loader.fetch_all() will consolidate this with the MTF
    # 1D fetch to eliminate the redundant call when skip_mtf=False.
    # Gate is FAIL-OPEN: if fetch/compute fails for any reason, sma200_above = None
    # and generate_signal() bypasses the gate rather than blocking valid trades.
    sma200_above = None
    sma200_val   = None       # native currency OF WHICHEVER FETCH produced it — see below
    sma200_is_global = None   # source of THAT fetch, not assumed to match the entry-TF fetch
    try:
        if actual_tf == "1D" and len(df) >= 200:
            sma200_val   = float(df["close"].rolling(200).mean().iloc[-1])
            sma200_above = price_native_ohlcv >= sma200_val
            sma200_is_global = is_global  # same df as the entry chart — same source, safe to reuse
        else:
            if asset_type == "crypto":
                df_1d, _e1d = fetch_ohlcv(ticker, "1D", 210, force_refresh=force_refresh)
            else:
                df_1d, _e1d, _ = fetch_stock(ticker, "1D", 210)
            if df_1d is not None and len(df_1d) >= 200:
                sma200_val   = float(df_1d["close"].rolling(200).mean().iloc[-1])
                sma200_above = price_native_ohlcv >= sma200_val
                # DASHBOARD FIX (chart SMA200 line): this is a SEPARATE fetch from
                # the entry-TF chart data — it can, in principle, land on a
                # different exchange (Binance vs Indodax fallback) than the main
                # chart did, even for the same ticker, especially now that each
                # fetch has its own independent retry/circuit-breaker (Step 1).
                # Checking df_1d's OWN attrs here, rather than reusing the entry
                # chart's `is_global`, is what prevents a repeat of the VWAP
                # currency-mismatch bug for this specific value.
                sma200_is_global = df_1d.attrs.get("source") == "binance"
    except Exception:
        pass   # sma200_above remains None → gate bypassed

    # Convert once, here, the same discipline as the TP/SL native-currency
    # conversion above: native -> IDR (if the SOURCE fetch was Binance) ->
    # the entry chart's own native currency (if THIS chart is_global).
    # These two "is_global" checks are deliberately independent — they can
    # disagree, and when they do, this is exactly why that matters.
    sma200_raw_native = None
    if sma200_val is not None and sma200_is_global is not None:
        _sma200_idr = sma200_val * rate if sma200_is_global else sma200_val
        sma200_raw_native = _sma200_idr / rate if is_global else _sma200_idr

    if skip_mtf:
        mtf = {
            "tier":"UNKNOWN","verdict":"MTF skipped","btc_bias":"N/A",
            "layers":{},"compass_tf":"1D","engine_tf":"4H","trigger_tf":actual_tf,
            "htf_bias":"unknown",
        }
    else:
        mtf = get_mtf_alignment(ticker, actual_tf, asset_type, trigger_df=df, force_refresh=force_refresh)

    htf    = mtf["htf_bias"]
    btc    = mtf["btc_bias"]
    signal = generate_signal(
        ticker, last, prev, structure, asset_type, regime, htf, btc,
        sma200_above=sma200_above, timeframe=actual_tf,
    )
    if price_divergence_warning:
        signal["warnings"].append(price_divergence_warning)
    for _dq_warning in data_quality_warnings:
        signal["warnings"].append(_dq_warning)
    risk   = size_position(price_idr, atr_idr, asset_type, rate, btc_bias=btc, score=signal["score"])
    # DASHBOARD FIX (chart TP/SL zones): convert the raw IDR levels above
    # into the SAME units as ohlcv_df's candlesticks — native USDT for a
    # Binance-sourced (is_global) ticker, already-IDR for Indodax-sourced
    # crypto or stocks. This is the ONE place this conversion happens.
    # Chart code reads *_raw_native directly and never touches rate/
    # is_global itself — re-deriving currency conversion in a display layer
    # is exactly the mistake that caused the 2026-07-08 VWAP display bug.
    if "error" not in risk:
        _chart_divisor = rate if is_global else 1.0
        for _k in ("entry", "sl", "tp1", "tp2"):
            risk[f"{_k}_raw_native"] = risk[f"{_k}_raw_idr"] / _chart_divisor
    entry_z= get_entry_zone(price_idr, atr_idr, structure, signal["score"], rate, is_global,
                             counter_trend=signal.get("counter_trend_valid", False))

    # P2-A Bug 13: price-denominated indicators stored as raw floats (not rounded to 2dp).
    # dual() / _pd_compact() in pretty_print() handle sub-cent display.
    _PRICE_RAW = {"ema9","ema21","ema50","macd_hist","bb_upper","bb_lower","atr","vwap",
                  "dc_upper","dc_lower"}
    FIELDS = [
        ("ema9",2),("ema21",2),("ema50",2),("rsi",1),("stoch_k",1),("stoch_d",1),
        ("macd_hist",2),("bb_upper",2),("bb_lower",2),("bb_width",4),
        ("atr",2),("obv",0),("adx",1),("vwap",2),("mfi",1),
        ("dc_upper",2),("dc_lower",2),
    ]
    ind = {}
    for k, dp in FIELDS:
        v = sf(last.get(k))
        if v is None:
            ind[k] = None
        elif k in _PRICE_RAW:
            ind[k] = v         # raw float — Bug 13 fix
        else:
            ind[k] = round(v, dp)
    ind.update({
        "vol_spike":        bool(last.get("vol_spike",    False)),
        "vol_strong":       bool(last.get("vol_strong",   False)),
        "bull_engulf":      bool(last.get("bull_engulf",  False)),
        "bear_engulf":      bool(last.get("bear_engulf",  False)),
        "dc_breakout_bull": bool(last.get("dc_breakout_bull", False)),
        "dc_breakout_bear": bool(last.get("dc_breakout_bear", False)),
        "avwap_cap":         last.get("avwap_cap"),
        "avwap_anchor_type": last.get("avwap_anchor_type"),
    })


    _src_base = df.attrs.get('source', '?')
    src = (f"{_src_base} (USDT→IDR @ {int(rate)}) [{live_tag}]"
           if asset_type == "crypto" else _src_base)

    # P2-A Bug 13: structure S/R display strings — dual() handles sub-cent assets.
    # resistance_usd / support_usd now contain full dual-format strings (Rp + USD),
    # despite the key name suggesting USD-only (kept for backward compat with screener).
    def _struct_dual(raw_val):
        if raw_val is None or raw_val == 0:
            return None
        fv = float(raw_val)
        if is_global:
            return dual(fv * rate, rate)   # Binance USD → IDR display with USD
        elif asset_type == "crypto":
            return dual(fv, rate)           # Indodax IDR → IDR with USD
        else:
            return dual(fv)                 # Stock IDR → IDR only

    # P2-A Bug 13: price_usd — precision-aware USD string for header display
    def _usd_hdr(v):
        if v is None: return None
        if v >= 1000:   return f"${v:,.2f}"
        elif v >= 0.01: return f"${v:,.4f}" if v < 1 else f"${v:,.2f}"
        elif v > 1e-9:
            raw = f"{v:.8f}".rstrip("0")
            return f"${raw}"
        return f"${v:.2e}"

    result = {
        "ticker":        ticker,
        "asset_type":    asset_type,
        "timeframe":     timeframe,
        "actual_tf":     actual_tf,
        "source":        src,
        "price_idr":     dual(price_idr, rate),
        "price_idr_raw": price_idr,
        "price_usd":     _usd_hdr(price_usd_val),     # Bug 13: precision-aware
        "atr_pct":       round(atr_pct_val, 2) if atr_pct_val else None,
        "rate":          f"1 USD = Rp {int(rate):,} ({rate_src})",
        "rate_raw":      rate,          # P2-A Bug 13: numeric rate for pretty_print
        "is_global":     is_global,     # P2-A Bug 13: asset is USD-denominated
        "sma200_above":  sma200_above,  # P2-B: True/False/None
        "sma200_raw_native": sma200_raw_native,  # DASHBOARD FIX: chart reference line
        # DASHBOARD FIX (Step 3): these three were already computed above —
        # rate_src and live_tag were only ever embedded inside formatted
        # strings ("rate", "source"), and data_quality_warnings (Step 2) was
        # only ever folded into signal["warnings"] text. Exposing them as
        # their own keys costs nothing new to compute — it just stops
        # discarding health signals a caller (e.g. a dashboard status strip)
        # would otherwise have to regex out of a display string.
        "rate_src":              rate_src,
        "live_tag":              live_tag,
        "data_quality_warnings": data_quality_warnings,
        "indicators":    ind,
        "volume": {
            "last":    round(float(last.get("volume",0))),
            "avg20":   round(vol_avg),
            "ratio":   round(last["volume_ratio"],3),
            "verdict": "😴" if last["volume_ratio"]<0.8 else ("🔥" if last["volume_ratio"]>2 else "✅"),
        },
        "structure": {
            "resistance_usd": _struct_dual(structure.get("resistance")),
            "support_usd":    _struct_dual(structure.get("support")),
            **structure,
        },
        "regime":      regime,
        "regime_note": rnote,
        "mtf":         mtf,
        "signal":      signal,
        "entry_zone":  entry_z,
        "trade_setup": risk,
        "news":        [],
        "candles":     len(df),
        # DASHBOARD FIX: analyze() previously computed a full indicator-laden
        # dataframe (calc_indicators()) and then kept only the LAST row's
        # scalar values above — df itself went out of scope on return, so
        # there was no series to draw a chart line through. This is additive
        # only: every existing key/behavior above is unchanged. Tail(200) to
        # keep the result dict light; adjust if a chart ever needs more history.
        "ohlcv_df":    df.tail(200),
    }

    # Signal logger — appends every completed analyze() call to CSV for Phase 5 calibration.
    # DASHBOARD FIX: log_signal=False skips this block entirely. Raising here
    # (instead of re-indenting everything below under an `if`) is deliberate —
    # this whole block already has a blanket `except Exception: pass` a few
    # lines down for "logging must never crash the main function", so this
    # reuses that existing catch rather than adding a second code path.
    try:
        if not log_signal:
            raise RuntimeError("log_signal=False — signal logging skipped")
        import csv, io, os, pathlib, time
        from datetime import datetime as _dt, timezone as _tz
        # BUG FIX (Finding S): was a bare relative path, resolving against
        # whatever the current working directory happened to be at runtime —
        # a scheduled task or wrapper script run from elsewhere would silently
        # fragment the signal history across multiple files. Anchored to the
        # script's own directory so it's always the same file regardless of
        # where the command is invoked from.
        log_path  = pathlib.Path(__file__).resolve().parent / "madbot_signals.csv"
        lock_path = log_path.with_suffix(".csv.lock")
        sig_data  = result.get("signal", {})

        # BUG FIX (audit finding, 2026-07-07): _SIGNAL_LOG_LOCK is a
        # threading.Lock — it only serializes threads inside ONE Python
        # process. It gives zero protection against a manual `analyze.py`
        # call and a `screener.py` batch run (two separate OS processes)
        # hitting this file at the same instant, which is exactly the
        # scenario your real usage pattern produces. Confirmed cause of the
        # observed corruption: rows with a numeric value split across two
        # physical lines (e.g. "1367.\n.0") and a "Falsee" typo — the
        # signature of two processes' write() calls interleaving mid-buffer.
        # Fix: a cross-process advisory lock via an exclusively-created
        # sidecar file (os.O_CREAT|O_EXCL is atomic on both Windows and
        # POSIX), with retry/backoff and stale-lock takeover so a crashed
        # process holding the lock can't permanently jam future runs.
        # The threading.Lock is KEPT alongside it (cheap, avoids pointless
        # inter-thread contention on the file lock within one process).
        def _acquire_file_lock(path, timeout=5.0, stale_after=30.0):
            deadline = time.time() + timeout
            while True:
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    return True
                except FileExistsError:
                    try:
                        if time.time() - os.path.getmtime(path) > stale_after:
                            os.remove(path)   # crashed process — steal the lock
                            continue
                    except OSError:
                        pass
                    if time.time() > deadline:
                        return False
                    time.sleep(0.05)

        def _release_file_lock(path):
            try:
                os.remove(path)
            except OSError:
                pass

        with _SIGNAL_LOG_LOCK:
            if _acquire_file_lock(lock_path):
                try:
                    write_hdr = not log_path.exists()
                    # BUG FIX (audit finding, 2026-07-07): build the entire
                    # row (and header, if needed) as ONE in-memory string and
                    # issue a SINGLE write() call. Two separate writer calls
                    # (header write, then row write) is two separate syscalls
                    # — a second window for another process to interleave
                    # even with the lock in place if the lock file itself
                    # were ever bypassed. One buffered write is materially
                    # harder to tear.
                    buf = io.StringIO()
                    _w  = csv.writer(buf)
                    # SCHEMA EXTENSION (Stage 0, 2026-08-13): adds SL/TP levels,
                    # counter_trend_valid, and the four normalized score
                    # components (trend/momentum/volume/structure) — all of
                    # which analyze() already computes and previously
                    # discarded before logging. Purely additive: every
                    # pre-existing column keeps its name and position: new
                    # columns are appended at the end, so this is the SAME
                    # "11-col -> 12-col" style schema bump load_source_rows()
                    # in annotate_outcomes.py was already built to expect —
                    # NOT a new kind of change to this pipeline.
                    #
                    # KNOWN, NECESSARY CONSEQUENCE: annotate_outcomes.py's
                    # load_source_rows() currently only recognizes 11- or
                    # 12-column rows and discards anything else as a bad row
                    # (see its own len(fields) == 11 / == 12 / else branch).
                    # Until that file is updated to recognize this new column
                    # count, every row logged by THIS version of analyze.py
                    # will be silently counted as a bad/unrecoverable row by
                    # the CURRENT annotate_outcomes.py — not corrupted, just
                    # invisible to it until both sides of this change ship
                    # together. Do not be alarmed by a bad_rows spike between
                    # shipping this file and updating annotate_outcomes.py;
                    # ship the annotate_outcomes.py schema update in the same
                    # session as this one, not "eventually."
                    #
                    # risk.get(...) defaults to "" for bearish signals (score
                    # <= -2) and the "ATR unavailable" case, where size_
                    # position() returns an {"error": ...} dict with no SL/TP
                    # keys at all — same fail-open convention as every other
                    # .get(..., default) call already in this block, not a
                    # new pattern.
                    if write_hdr:
                        _w.writerow([
                            "timestamp","ticker","timeframe","actual_tf",
                            "score","bias","regime","htf","btc",
                            "confidence","entry_price_idr","sma200_above",
                            "sl_raw_idr","tp1_raw_idr","tp2_raw_idr",
                            "counter_trend_valid",
                            "component_trend","component_momentum",
                            "component_volume","component_structure",
                        ])
                    _components = sig_data.get("components", {}) or {}
                    _w.writerow([
                        _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        result["ticker"],
                        result["timeframe"],
                        actual_tf,
                        sig_data.get("score", 0),
                        sig_data.get("bias", ""),
                        sig_data.get("regime", ""),
                        sig_data.get("htf_bias", ""),
                        sig_data.get("btc_bias", ""),
                        sig_data.get("confidence", ""),
                        result.get("price_idr_raw", 0),
                        sma200_above,
                        risk.get("sl_raw_idr", ""),
                        risk.get("tp1_raw_idr", ""),
                        risk.get("tp2_raw_idr", ""),
                        sig_data.get("counter_trend_valid", False),
                        _components.get("trend", ""),
                        _components.get("momentum", ""),
                        _components.get("volume", ""),
                        _components.get("structure", ""),
                    ])
                    with open(log_path, "a", newline="", encoding="utf-8") as _f:
                        _f.write(buf.getvalue())
                finally:
                    _release_file_lock(lock_path)
            # else: could not acquire lock within timeout — skip this log
            # entry rather than risk a corrupting concurrent write. Logging
            # must never block or crash the main function.
    except Exception:
        pass   # logging must never block or crash the main function

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 10 — CLI
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="MadBot Analyzer v7.0",
        epilog=(
            "Examples:\n"
            "  python analyze.py BTC              # pretty print, 1H\n"
            "  python analyze.py BTC 4H           # 4H timeframe\n"
            "  python analyze.py BBCA 1D          # IDX stock\n"
            "  python analyze.py BTC --raw        # JSON output\n"
            "  python analyze.py BTC --no-mtf     # skip 3-TF fetch (faster)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("ticker")
    p.add_argument("timeframe",    nargs="?", default="1H")
    p.add_argument("--asset-type", "-at",     choices=["crypto","stock"], default=None)
    p.add_argument("--raw",        action="store_true", help="JSON output")
    p.add_argument("--no-mtf",     action="store_true", help="Skip 3-TF fetch")
    args = p.parse_args()

    tf  = TF_NORM.get(args.timeframe.lower(), args.timeframe)
    res = analyze(args.ticker, tf, args.asset_type, skip_mtf=args.no_mtf)

    if "error" in res:
        print(f"\n  ERROR: {res['error']}\n")
        sys.exit(1)

    if args.raw:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        pretty_print(res)