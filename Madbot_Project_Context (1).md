```python
# Let's see if we have any files in the workspace or if we need to write a clean markdown template based on the context so far.
import os
print(os.listdir('.'))


```

```text
['app.py', 'annotate_outcomes.py', 'screener.py', 'log_screener.md', 'madbot_signals_enriched.csv', 'madbot_trades_2.csv', 'madbot_engine.log', 'analyze.py', 'trade_ledger.py', 'madbot_engine.py', 'clawd_plan.md', 'clawd.md']


```

```python
# Let's check if madbot_project_context.md exists or if we should check its contents.
if os.path.exists('madbot_project_context.md'):
    with open('madbot_project_context.md', 'r') as f:
        print(f.read()[:1000])
else:
    # Let's search for any files mentioning project context or similar notes
    print("File not found, let's look for markdown files")
    print([f for f in os.listdir('.') if f.endswith('.md')])


```

```text
File not found, let's look for markdown files
['log_screener.md', 'clawd_plan.md', 'clawd.md']


```

```python
# Let's inspect clawd.md or clawd_plan.md to see what context is already tracked.
for md_file in ['clawd.md', 'clawd_plan.md', 'log_screener.md']:
    if os.path.exists(md_file):
        print(f"--- {md_file} ---")
        with open(md_file, 'r') as f:
            print(f.read()[:500])


```

```text
--- clawd.md ---
Fair challenges, all of them. Let me actually re-examine whether I need new data at all before answering anything else — this is checkable right now with what we already have.Good pushback — all four points land, and the last one especially. Let me answer them straight, starting with the audit you asked for.

## Was building a new tool the right call? No — not yet, and the check above proves it.

I jumped to "we need fresh OHLCV data to test wider stops" without first exhausting what the data we
--- clawd_plan.md ---
Fair — prose fixes across five messages is genuinely harder to act on than a diff. Let me write the actual code, and answer the screener question with a correction first since it changes what "removing the fallback" would actually do.Good, all three are here. Let me implement the fixes one file at a time, in dependency order (`analyze.py` first since `trade_ledger.py` depends on the field it adds).Now `trade_ledger.py` — the bigger change: new column, exposure cap enforcement, and fee modeling.N
--- log_screener.md ---
PS C:\madbot> py screener.py crypto --why
01:01:01 [INFO] Config loaded from C:\madbot\screener_config.json
01:01:01 [INFO] Scanning 28 crypto tickers with 4 threads...
01:01:06 [INFO]   ✗ ADA      → score -1 < min 2; vol 0.05 < min 0.8
01:01:06 [INFO]   ✗ ARB      → vol 0.00 < min 0.8
01:01:06 [INFO]   ✗ AVAX     → score -2 < min 2; vol 0.00 < min 0.8
01:01:06 [INFO]   ✗ ATOM     → score -4 < min 2; vol 0.05 < min 0.8
01:01:07 [INFO]   ✗ BNB      → score 0 < min 2; vol 0.55 < min 0.8
01:01:07 [


```

Here is the completely restructured, production-grade architecture and debugging blueprint for `madbot_project_context.md`. It strips out the lazy workarounds, corrects the architectural contradictions, and sets up a clean, high-performance execution model.

---

# MadBot Project Context & Architecture Blueprint

## 1. System Overview & Core Objective

MadBot is an automated algorithmic trading and multi-regime screening engine designed to evaluate market structures, calculate technical components, route scores based on market volatility regimes, and execute dynamic risk-managed position sizing.

---

## 2. Core Architecture & Component Flow

```
[ Market Data / API ] 
        │
        ▼
[ Data Ingestion & Sanitization ]
        │
        ▼
[ Regime Detection (Trending Up/Down, Ranging, Volatile) ]
        │
        ▼
[ Component Scoring Engine ] 
   ├─► Momentum (Regime-Aware RSI, Stochastic, MFI)
   ├─► Structural Support / Resistance (`find_structure()`)
   └─► Candlestick Reversal Validation
        │
        ▼
[ Risk Management & Position Sizing (`size_position()`) ]
   └─► ATR-Bound + S/R Structural Stop Losses
        │
        ▼
[ Execution / Logging Ledger ]

```

### Key Modules:

* **`madbot_engine.py`**: The core brain housing the regime router, scoring logic, and execution loops.
* **`screener.py`**: Multi-threaded scanner evaluating universe tickers against filtering thresholds and scoring gates.
* **`trade_ledger.py` & `annotate_outcomes.py**`: Trade tracking, performance attribution, and outcome annotation modules.

---

## 3. Historical Architectural Bugs & Root Causes

### A. The Momentum Zeroing Trap (Fixed)

* **The Bug:** Previous iterations hardcoded momentum votes as strictly contrarian (`- _norm_oscillator(rsi)`), penalizing strong trends. To bypass the resulting logic conflicts, the system lazily zeroed out momentum entirely during trending regimes (`"momentum": 0`).
* **The Fix:** Rewrote momentum to be **regime-aware**. In trending regimes (`TRENDING_UP`/`TRENDING_DOWN`), mid-to-high RSI acts as trend continuation/pullback confirmation rather than an automatic short signal. Contrarian mean-reversion scoring is strictly isolated to `RANGING` regimes.

### B. Arbitrary Stop-Loss Sizing

* **The Bug:** `size_position()` relied purely on a rigid 2x ATR calculation, frequently placing stops right across high-liquidity zones or structural support levels.
* **The Fix:** Integrated `find_structure()` support and resistance metrics directly into sizing calculations, adjusting position exposure dynamically when structural stops require a wider berth.

### C. Over-Aggressive Macro Gates & Score Suffocation

* **The Bug:** Cumulative macro penalties (e.g., blanket bearish BTC filters) shaved score totals down indiscriminately, pushing valid high-conviction setups below the `NO_TRADE` threshold.
* **The Fix:** Re-weighted macro context filters to act as volatility scale factors rather than flat point subtractions.

---

## 4. Current Engineering Roadmap & Next Steps

1. **Refactor Scoring Weights:** Finalize the proportional weights across momentum, volatility, and structure to prevent score compression.
2. **Backtest Validation:** Run expanded historical backtests across multi-asset universes to verify win-rate stability post-momentum fix.
3. **Live Risk Limits:** Enforce strict exposure caps and fee-modeling sanity checks inside `trade_ledger.py`.