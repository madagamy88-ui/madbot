# MadBot — Technical Handoff Summary

**Purpose of this document:** context for any LLM/session picking this project up cold. Read this before touching any code or making claims about the system's performance.

---

## 1. What MadBot Is

A rules-based (non-ML) algorithmic trading signal system for crypto (Binance/Indodax data, intended execution on Tokocrypto) and IDX stocks, position sizing in IDR. Built solo since ~May 2026. Currently NOT automated — signals are logged, not auto-executed. Owner is evaluating whether the system has a real trading edge before considering automation.

**Core files:**
- `analyze.py` — single-ticker signal generation: regime detection, 4-component normalized scoring (trend/momentum/volume/structure), confidence grading, position sizing, TP/SL calculation
- `screener.py` — batch scanner across a dynamic crypto/stock watchlist
- `trade_ledger.py` — CSV trade logging, position lifecycle, outcome resolution
- `madbot_engine.py` — automated 15-min cycle runner (calls screener + ledger)
- `annotate_outcomes.py` — offline tool that walks forward OHLCV data to annotate historical signals with real outcomes (path-aware: HIT_SL/HIT_TP1/HIT_TP2)
- `app.py` — Streamlit dashboard ("Command Center")

---

## 2. Current Statistical State of the System — READ THIS FIRST

Based on the most recently recovered, verified ledger (317 rows: 309 closed, 8 open):

- **Win rate: 49.84% (154W/155L). Gross total: +18.0R. Gross avg: +0.058R/trade.**
- **This has been trending toward zero across three separate checkpoints as data accumulated: 55.88% WR / +45.0R (n=238) → 49.67% / +17.0R (n=304) → 49.84% / +18.0R (n=309).** This pattern (apparent edge shrinking toward a coin flip as N grows) is classic regression to the mean — evidence AGAINST a real edge, not for one.
- **Day-clustered significance test** (treats each independent trading day as one observation, not each trade, because trades on correlated crypto assets aren't independent draws): **not statistically significant, and now trending negative** (mean daily R = -0.05, t=-0.32, p=0.75, only 6/16 days net positive).
- **Net of real, confirmed Tokocrypto fees: NEGATIVE under every tested scenario.** Official fee schedule confirmed via `support.tokocrypto.com`: IDR pairs ~0.65% round trip (taker), ~0.45% (maker); USDT/crypto pairs ~0.81% round trip (no maker discount). Applied to this dataset: net total ranges from **-42.6R to -89.9R** depending on pair/order type. **No regime survives fees — not even the best-performing one (SQUEEZE, gross +0.21R avg) which goes to -0.29R net.**
- **One candidate lead investigated and found likely NOT real:** TRENDING_UP regime + confidence grade D or B (n=103) showed positive net-of-fee average (+0.099R and +0.052R). Mechanism-checked by breaking down avg R by exact score value within TRENDING_UP — found a non-monotonic, "sawtooth" pattern (score 2: +0.38, score 3: +0.79, score 4: -0.56, score 7: +0.54, score 8: -0.22, score 9: -0.19) rather than a clean trend. This means D/B's positive average is likely an artifact of 1-2 lucky score-buckets, not a real "low conviction beats high conviction" effect. **Do not build a filter around specific score values or this grade combination — that would repeat an overfitting mistake already identified once in this project** (an external AI model's earlier suggestion to "optimize for scores 0, 7, 9" was rejected for the same reason).

**Bottom line: no confirmed edge. Net of real fees, this configuration would have lost money over the observed period. The system is not ready for automation or real capital deployment.**

---

## 3. Files Audited, Bugs Found and Fixed (in dependency order)

### `analyze.py`
| Fix | What | Status |
|---|---|---|
| `size_position()` now returns `money_raw_idr`, `risk_raw_idr` | Previously only a formatted display string existed for position size; nothing could compute an exposure cap without the raw number | Shipped, verified |
| `_resilient_call()`: bad/unlisted ticker symbols raise immediately instead of tripping the shared circuit breaker | A single malformed ticker was fail-fasting every other ticker in the same scan for 60s (confirmed via scan timing in a real log) | Shipped, verified against installed ccxt (uses real `ccxt.BadSymbol`, not the nonexistent `ccxt.SymbolNotFound` initially proposed) |
| `price_divergence_warning`: normalizes Indodax IDR price to USD before comparing to the always-USD live Binance ticker | Was comparing IDR directly to USD, producing a spurious, uniform "100.0%" warning on every ticker | Shipped, verified with real numbers (old formula reproduces exactly 100.0%, new formula gives correct ~0.2%) |

**NOT YET DONE:** `analyze.py` does **not** calculate fees anywhere — confirmed via direct grep, zero hits. All fee logic lives in `trade_ledger.py`.

### `screener.py`
| Fix | What | Status |
|---|---|---|
| `fetch_dynamic_crypto_watchlist()`: reject non-ASCII/non-alphanumeric ticker symbols at two points | A malformed ticker (observed as garbled non-ASCII characters in a real scan log) should never have reached `analyze()` — likely root trigger for the circuit-breaker bug above | Shipped, verified |

**NOT AUDITED:** no deep bug-hunting pass beyond the ticker-sanity fix above. `min_score`/`crypto_dynamic_top`/watchlist config values reviewed conceptually but not stress-tested.

### `trade_ledger.py` — the most heavily modified file, includes a real incident
| Fix | What | Status |
|---|---|---|
| New columns: `position_size_idr`, `net_r_multiple` | Records actual position size at open; fee-adjusted return alongside untouched gross figure | Shipped |
| `get_committed_capital()` + exposure check inside `log_trade_open()` | Portfolio-level exposure cap — confirmed real bug: 20 concurrent positions against a design supporting 5 | Shipped |
| `ROUND_TRIP_FEE_PCT` (placeholder, currently 0.4% flat) + `_fee_as_r()` | Fee modeling — **placeholder only, needs to become pair-aware** since the owner trades both IDR and USDT pairs with different fee schedules (0.45-0.65% vs 0.81% round trip) | **Shipped but flagged incomplete — awaiting owner sign-off before further changes to this file, given the incident below** |
| `_migrate_schema_if_needed()`, `_OLD_TRADE_COLS_27`, `_OLD_TRADE_COLS_28_WITH_POSITION_SIZE` | Schema migration guard | **See incident below — this caused real, since-recovered data corruption** |

**REAL INCIDENT — read before touching this file's schema logic again:** the column-addition fix above caused a genuine data corruption event. Root cause: the schema migration function used a wrong historical column-order constant (`_OLD_TRADE_COLS_27` had `last_checked_at` in the wrong position), which mislabeled `status`, `exit_reason`, and `r_multiple` across 308 of 317 real rows. **Diagnosed and fixed via forensic reconstruction** — verified 5 independent ways (TP1/TP2 algebraic relationship intact for 100% of rows, status/exit_reason validity, R-multiple sign consistency vs exit reason, and the exact same 6 originally-corrupted rows from the very first audit landing back in their known state). Data fully recovered, file corrected, migration function fixed and re-verified against a reconstructed copy of the true pre-corruption state. **Any future schema change to this file needs the same level of verification before shipping — do not assume a migration is correct without testing it against the actual corrupted shape, not a clean hypothetical.**

### `madbot_engine.py`
| Fix | What | Status |
|---|---|---|
| Parse-failure counter in `flag_stale_trades()` | Silent row loss with no accounting | Shipped |
| Clarified log message when `log_trade_open()` returns `False` | Now has two causes (lock timeout vs exposure cap), message says to check stdout | Shipped |

No corruption risk in this file — it never writes the CSV directly, only calls into `trade_ledger.py`.

### `app.py` (dashboard)
**NOT FORMALLY AUDITED BY CLAUDE.** Owner has visually confirmed the dashboard displays correctly against the recovered data ("csv and dashboard was accurate") but no line-level code review has been done on this file. It reads columns by name (not position), so it should be compatible with the schema fixes, but this has not been independently verified.

### `annotate_outcomes.py`
**NOT AUDITED with the same rigor as the other four files.** Read and understood early in this project (used as a reference pattern for how schema-drift guards should work — ironically, its existing `_read_existing_header()` pattern is what `trade_ledger.py`'s migration function should have copied more carefully). No dedicated bug-hunting pass has been done on this file itself.

**Known, still-open issue:** a schema mismatch exists between `analyze.py`'s signal logger and this file's own `EXPECTED_COLS` — flagged previously, not yet resolved, silently accumulating stale/unparseable data in the meantime. This should be the first thing checked if this file is audited.

---

## 4. Known Open Items / Explicitly Flagged, Not Yet Resolved

1. **`ROUND_TRIP_FEE_PCT` needs to become pair-type-aware** (IDR vs USDT, maker vs taker) — the ledger doesn't currently record which pair type a given trade used, so retroactive precision isn't possible; needs to be logged going forward. Owner has been asked for sign-off before this file is touched again given the recent incident.
2. **Tokocrypto API surface is unresolved** — two different technical references found (a CCXT-compatible Binance-style API, and a separate `developer.tcdx.id` API with its own HMAC auth scheme). Never pinned down which is current/correct. Deprioritized since there's no confirmed edge to automate yet.
3. **No portfolio-level exposure test has been run against a live exchange** — the cap exists in code, has been unit-tested with synthetic data, but never exercised against real Tokocrypto order placement (no live execution exists yet).
4. **TRENDING_UP + grade D/B lead** — flagged as likely noise (see Section 2) but not definitively closed. If tracking forward, use the day-clustered t-test methodology already established, on NEW data only, never retroactively.
5. **`analyze.py` signal logger vs. `annotate_outcomes.py`'s `EXPECTED_COLS` schema mismatch** — flagged in earlier work, not yet resolved. Silently accumulating stale/unparseable rows in `madbot_signals_enriched.csv` until fixed.
6. **Windows Task Scheduler battery/power management fix — deployment status unclear.** A prior session diagnosed the engine silently skipping cycles when the machine is on battery, and prescribed a PowerShell-based fix (the legacy `schtasks` CLI can't set this) to be baked into `setup_madbot.bat`. Whether this was actually deployed has not been confirmed. Directly relevant to the earlier battery-death incident in this project's history — worth checking before assuming the engine is resilient to that failure mode now.
7. **Dashboard lag, root cause identified but not yet fixed:** `check_and_update_open_trades()` runs at module scope in `app.py` on every Streamlit rerun, doing sequential (non-threaded) API calls with a fixed 1000-candle fetch regardless of trade age. When the 5-minute per-trade throttle window expires for several trades at once, this produces a burst of blocking sequential fetches. Fix path: adopt the dynamic fetch-window sizing already used in `annotate_outcomes.py`'s `_fetch_forward_window()`, and consider threading this loop the way `screener.py` already threads its scan.

---

## 5. Next Tasks, Prioritized

1. **Do not automate. Do not deploy real capital against this configuration.** The net-of-fees finding is decisive enough to act on now.
2. **Audit `app.py`** with the same line-level rigor already applied to the other four files — currently the biggest gap in verification coverage relative to how much the owner relies on it.
3. **Audit `annotate_outcomes.py`** with the same rigor, particularly since it's the source of the larger (6,538-row) enriched signal dataset used for regime/asset-level analysis earlier in this project.
4. **Resolve the fee constant's pair-awareness** with the owner's input on their actual real trade mix (IDR vs USDT pairs), then log pair-type going forward so future fee attribution can be precise instead of a blended estimate.
5. **If continuing to look for a real edge:** treat this as starting closer to scratch than "refine what's there" — the four-component scoring system, regime routing, and confidence grading are structurally sound (verified: components show expected correlation patterns mitigated by regime-weight routing) but have not produced a validated edge after ~3 months of live signal collection. Consider whether a structurally simpler system (fewer parameters, less room for spurious sub-population patterns to hide in) is a more tractable path than continuing to add complexity to this one.
6. **Any code change to `trade_ledger.py` specifically** should be treated with elevated caution given the incident in Section 3 — verify against reconstructed real corrupted data, not synthetic test cases, before shipping.
