"""
MadBot Dashboard — Command Center & Market Screener
================================================================================
  - Fix: Root logger initialized BEFORE Streamlit to prevent hijack.
  - Fix: EMA21/VWAP strictly respect is_global currency context via dual().
  - Fix: Execution Matrix properly segments Weak Bull vs Neutral vs Bearish.
  - Refactor: All storage & resolver functions imported from trade_ledger.py.
  - Preserved: Home, Screener, Command Center, MTF Sparklines, Plotly Charting Engine.
"""

import logging
# ── INITIALIZE ROOT LOGGER BEFORE EVERYTHING ELSE ────────────────────────────
logging.basicConfig(level=logging.CRITICAL)

import time
import json
import re
import os
import csv
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sys, pathlib

st.set_page_config(page_title="MadBot Command Center", layout="wide", initial_sidebar_state="expanded")

# ── GLOBAL CSS INJECTION (Blankets the entire app) ───────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400;1,700&family=Syncopate:wght@400;700&display=swap');

/* Headers */
h1, h2, h3, h4, h5 {
    font-family: 'Syncopate', sans-serif !important;
    text-transform: uppercase;
    color: #e0e0e0 !important;
    letter-spacing: 1px;
}

/* MadBot OS title specific */
h1 {
    font-size: 2.5rem !important;
    margin-bottom: 0px !important;
    padding-bottom: 0px !important;
}

.subtitle-cyber {
    font-family: 'Space Mono', monospace;
    color: #8892b0;
    font-size: 0.85rem;
    margin-top: -5px;
    margin-bottom: 2rem;
}

/* Live Badge */
.live-badge {
    color: #26a69a;
    font-family: 'Space Mono', monospace;
    font-size: 0.85rem;
    font-weight: bold;
    float: right;
    margin-top: 20px;
}

/* Metrics */
[data-testid="stMetricValue"] {
    color: #00f0ff !important;
    font-family: 'Space Mono', monospace !important;
    font-weight: 700 !important;
}
[data-testid="stMetricLabel"] {
    color: #8892b0 !important;
    font-family: 'Space Mono', monospace !important;
    text-transform: uppercase;
    font-size: 0.75rem !important;
}

/* Base Body Text (Forces monospace on most standard text) */
p, div {
    font-family: 'Space Mono', monospace;
}

/* Signal Pills */
.signal-pill { 
    display: inline-block; padding: 3px 8px; border-radius: 4px; 
    font-size: 0.7rem; font-weight: 700; margin-right: 6px; margin-bottom: 4px; 
    background-color: rgba(255,255,255,0.05); color: #e0e0e0; border: 1px solid rgba(255,255,255,0.1); 
    white-space: nowrap;
}
.pill-bullish { border-left: 4px solid #26a69a; color: #26a69a; background-color: rgba(38,166,154,0.1); }
.pill-bearish { border-left: 4px solid #ef5350; color: #ef5350; background-color: rgba(239,83,80,0.1); }
.pill-rsi { border-left: 4px solid #ab47bc; color: #ab47bc; background-color: rgba(171,71,188,0.1); }
.pill-momentum { border-left: 4px solid #00f0ff; color: #00f0ff; background-color: rgba(0,240,255,0.1); }
.pill-neutral { border-left: 4px solid #9e9e9e; color: #9e9e9e; background-color: rgba(158,158,158,0.1); }

/* Score Circle */
.score-circle { 
    display: inline-block; width: 26px; height: 26px; line-height: 24px; 
    border-radius: 50%; text-align: center; font-weight: bold; border: 1px solid; 
}

/* Metric Pills for Telemetry Panel */
.metric-pill {
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(255,255,255,0.03); padding: 8px 12px;
    border-radius: 4px; margin-bottom: 6px; border: 1px solid rgba(255,255,255,0.05);
}
.pill-label { color: #8892b0; font-size: 0.75rem; text-transform: uppercase; font-family: 'Space Mono', monospace; }
.pill-value { color: #e0e0e0; font-family: 'Space Mono', monospace; font-size: 0.85rem; font-weight: 600; text-align: right;}
</style>
""", unsafe_allow_html=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from analyze import analyze, dual, fetch_ohlcv, fetch_stock, fetch_rate
from screener import screen, load_config

# ── DECOUPLED TRADE LEDGER MODULE ─────────────────────────────────────────────
from trade_ledger import (
    load_trades, 
    log_trade_open, 
    close_trade, 
    check_and_update_open_trades, 
    TRADES_LOG, 
    _TRADE_COLS
)

# ── Session memory & Routing State ───────────────────────────────────────────
if "analysis_cache" not in st.session_state:
    st.session_state.analysis_cache = {}
if "last_key" not in st.session_state:
    st.session_state.last_key = None
if "screener_results" not in st.session_state:
    st.session_state.screener_results = None
if "last_scan_duration" not in st.session_state:
    st.session_state.last_scan_duration = 0.0
if "cmd_ticker" not in st.session_state:
    st.session_state.cmd_ticker = "BTC"
if "cmd_tf" not in st.session_state:
    st.session_state.cmd_tf = "1H"
if "nav_radio" not in st.session_state:
    st.session_state.nav_radio = "🏠 Home"
if "trigger_analysis" not in st.session_state:
    st.session_state.trigger_analysis = False

# ── GLOBAL RESOLVER (Throttled natively via trade_ledger.py) ─────────────────
check_and_update_open_trades()

# ── Callbacks ────────────────────────────────────────────────────────────────
def switch_page(page_name):
    st.session_state.nav_radio = page_name

def launch_in_command_center(tkr, tf):
    st.session_state.cmd_ticker = tkr
    st.session_state.cmd_tf = tf
    st.session_state.nav_radio = "🎯 Command Center"
    st.session_state.trigger_analysis = True

# ── Sidebar Navigation & Overlays ────────────────────────────────────────────
with st.sidebar:
    st.markdown("<h2 style='color:#00f0ff; font-family:\"Syncopate\", sans-serif;'>MadBot OS</h2>", unsafe_allow_html=True)
    
    page = st.radio(
        "INTERFACE", 
        ["🏠 Home", "📡 Screener", "🎯 Command Center", "📊 Ledger"],
        key="nav_radio"
    )
    
    st.divider()
    
    st.subheader("Chart Presets")
    st.caption("Trend")
    show_ema9   = st.checkbox("EMA 9", value=False)
    show_ema21  = st.checkbox("EMA 21", value=True)
    show_ema50  = st.checkbox("EMA 50", value=False)
    show_vwap   = st.checkbox("VWAP", value=True)
    show_sma200 = st.checkbox("SMA 200", value=False)
    st.caption("Volatility")
    show_bb       = st.checkbox("Bollinger Bands", value=False)
    show_donchian = st.checkbox("Donchian Channel", value=False)
    st.caption("Structure")
    show_sr    = st.checkbox("Support / Resistance", value=True)
    show_tpsl  = st.checkbox("TP/SL Zone", value=True)
    show_avwap = st.checkbox("AVWAP Anchor", value=False)

LAYERS = {
    "ema9": show_ema9, "ema21": show_ema21, "ema50": show_ema50,
    "vwap": show_vwap, "sma200": show_sma200,
    "bb": show_bb, "donchian": show_donchian,
    "sr": show_sr, "tpsl": show_tpsl, "avwap": show_avwap,
}

_UP, _DOWN, _NEUTRAL = "#26a69a", "#ef5350", "#9e9e9e"
_TREND_COLORS = {
    "ema9": "#42a5f5", "ema21": "#ffa726", "ema50": "#ab47bc",
    "vwap": "#26c6da", "sma200": "#ffca28",
}

def generate_signal_pills(signals: list) -> str:
    html = ""
    for s in signals:
        s_lower = s.lower()
        clean_text = s
        if "trend context: bullish" in s_lower: clean_text = "bullish"
        elif "trend context: bearish" in s_lower: clean_text = "bearish"
            
        if any(w in s_lower for w in ["bullish", "rising", "breakout", "accelerating"]): css_class = "pill-bullish"
        elif any(w in s_lower for w in ["bearish", "fading", "down", "compression"]): css_class = "pill-bearish"
        elif "rsi" in s_lower: css_class = "pill-rsi"
        elif any(w in s_lower for w in ["overbought", "oversold"]): css_class = "pill-momentum"
        else: css_class = "pill-neutral"
        
        if len(clean_text) > 22: clean_text = clean_text[:19] + "..."
        html += f"<span class='signal-pill {css_class}'>{clean_text}</span>"
    return html

def render_telemetry_pill(label: str, value: str) -> str:
    return f"<div class='metric-pill'><span class='pill-label'>{label}</span><span class='pill-value'>{value}</span></div>"

def build_sparkline(closes: list, color: str) -> go.Figure:
    fig = go.Figure(go.Scatter(y=closes, mode="lines", line=dict(width=1.8, color=color)))
    fig.update_layout(
        height=45, margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        showlegend=False, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig

def build_chart(result: dict, ticker: str, timeframe: str, layers: dict, trades: list = None) -> go.Figure:
    df          = result["ohlcv_df"]
    structure   = result.get("structure", {}) or {}
    trade_setup = result.get("trade_setup", {}) or {}
    signal      = result.get("signal", {}) or {}
    indicators  = result.get("indicators", {}) or {}
    tradeable   = signal.get("tradeable", False)

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.6, 0.2, 0.2],
        vertical_spacing=0.03,
        specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}]]
    )

    end_labels = []
    last_x = df.index[-1]

    if layers.get("bb") and {"bb_upper", "bb_lower"} <= set(df.columns):
        fig.add_trace(go.Scatter(x=df.index, y=df["bb_upper"], line=dict(width=1, color="rgba(150,150,150,0.5)"), showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["bb_lower"], line=dict(width=1, color="rgba(150,150,150,0.5)"), fill="tonexty", fillcolor="rgba(150,150,150,0.05)", showlegend=False), row=1, col=1)

    if layers.get("donchian") and {"dc_upper", "dc_lower"} <= set(df.columns):
        fig.add_trace(go.Scatter(x=df.index, y=df["dc_upper"], line=dict(width=1, color="rgba(255,202,40,0.55)", dash="dot"), showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["dc_lower"], line=dict(width=1, color="rgba(255,202,40,0.55)", dash="dot"), showlegend=False), row=1, col=1)

    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=ticker, increasing_line_color=_UP, decreasing_line_color=_DOWN,
    ), row=1, col=1, secondary_y=False)

    for key in ("ema9", "ema21", "ema50"):
        if layers.get(key) and key in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df[key], line=dict(width=1.3, color=_TREND_COLORS[key]), name=key.upper()), row=1, col=1)
            end_labels.append({"name": key.upper(), "val": df[key].iloc[-1], "color": _TREND_COLORS[key]})

    if layers.get("vwap") and "vwap" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["vwap"], line=dict(width=1.3, color=_TREND_COLORS["vwap"], dash="dot"), name="VWAP"), row=1, col=1)
        end_labels.append({"name": "VWAP", "val": df["vwap"].iloc[-1], "color": _TREND_COLORS["vwap"]})

    if layers.get("sma200") and result.get("sma200_raw_native") is not None and len(df) >= 2:
        sma_v = result["sma200_raw_native"]
        fig.add_trace(go.Scatter(x=[df.index[0], last_x], y=[sma_v, sma_v], line=dict(width=1.3, color=_TREND_COLORS["sma200"], dash="dash"), name="SMA200"), row=1, col=1)
        end_labels.append({"name": "SMA200", "val": sma_v, "color": _TREND_COLORS["sma200"]})

    if layers.get("avwap"):
        avwap_v = indicators.get("avwap_cap")
        avwap_type = indicators.get("avwap_anchor_type")
        if avwap_v is not None and len(df) >= 2:
            c = _UP if avwap_type == "bullish_capitulation" else _DOWN
            fig.add_trace(go.Scatter(x=[df.index[0], last_x], y=[avwap_v, avwap_v], line=dict(width=1.3, color=c, dash="dashdot"), name="AVWAP"), row=1, col=1)
            end_labels.append({"name": "AVWAP", "val": avwap_v, "color": c})

    if layers.get("sr"):
        res, sup = structure.get("resistance"), structure.get("support")
        if res: fig.add_hline(y=res, line=dict(color=_DOWN, dash="dash", width=1), annotation_text="Resistance", annotation_position="top right", row=1, col=1)
        if sup: fig.add_hline(y=sup, line=dict(color=_UP, dash="dash", width=1), annotation_text="Support", annotation_position="bottom right", row=1, col=1)

    if layers.get("tpsl") and trade_setup and "error" not in trade_setup and tradeable and len(df) >= 2:
        entry, sl, tp2 = trade_setup.get("entry_raw_native"), trade_setup.get("sl_raw_native"), trade_setup.get("tp2_raw_native")
        future_x = last_x + (df.index[-1] - df.index[-2]) * 15
        if entry and sl:
            fig.add_shape(type="rect", x0=last_x, x1=future_x, y0=sl, y1=entry, fillcolor="rgba(239,83,80,0.14)", line_width=0, row=1, col=1)
        if entry and tp2:
            fig.add_shape(type="rect", x0=last_x, x1=future_x, y0=entry, y1=tp2, fillcolor="rgba(38,166,154,0.14)", line_width=0, row=1, col=1)

    if trades:
        rate_raw = result.get("rate_raw", 1.0) or 1.0
        is_global = result.get("is_global", False)
        _tf_secs = {"1m":60,"5m":300,"15m":900,"1H":3600,"4H":14400,"1D":86400,"1W":604800}.get(timeframe, 3600)
        for t in trades:
            try:
                entry_idr = float(t["entry_price_idr"])
                logged_ts = pd.Timestamp(t["logged_at"])
            except (TypeError, ValueError, KeyError):
                continue
            entry_native = entry_idr / rate_raw if is_global else entry_idr
            pos = df.index.get_indexer([logged_ts], method="nearest")[0]
            if pos < 0:
                continue
            nearest_ts = df.index[pos]
            if abs((nearest_ts - logged_ts).total_seconds()) > _tf_secs * 10:
                continue
            fig.add_trace(go.Scatter(
                x=[nearest_ts], y=[entry_native], mode="markers",
                marker=dict(symbol="triangle-up", size=13, color="#00f0ff",
                            line=dict(width=1, color="#0b0e14")),
                name="Trade logged", showlegend=False,
                hovertext=f"Logged {t.get('ticker','')} @ {dual(entry_idr, rate_raw)}",
                hoverinfo="text",
            ), row=1, col=1, secondary_y=False)

            if t.get("status") == "CLOSED" and t.get("exit_price_idr"):
                try:
                    exit_idr = float(t["exit_price_idr"])
                    closed_ts = pd.Timestamp(t["closed_at"])
                except (TypeError, ValueError, KeyError):
                    continue
                exit_native = exit_idr / rate_raw if is_global else exit_idr
                epos = df.index.get_indexer([closed_ts], method="nearest")[0]
                if epos < 0:
                    continue
                e_nearest = df.index[epos]
                if abs((e_nearest - closed_ts).total_seconds()) > _tf_secs * 10:
                    continue
                won = exit_idr >= entry_idr
                fig.add_trace(go.Scatter(
                    x=[e_nearest], y=[exit_native], mode="markers",
                    marker=dict(symbol="x", size=11, color=_UP if won else _DOWN,
                                line=dict(width=1, color="#0b0e14")),
                    name="Trade closed", showlegend=False,
                    hovertext=f"Closed: {t.get('exit_reason','')} @ {dual(exit_idr, rate_raw)}",
                    hoverinfo="text",
                ), row=1, col=1, secondary_y=False)

    vol_colors = [_UP if c >= o else _DOWN for c, o in zip(df["close"], df["open"])]
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], marker_color=vol_colors, name="Volume", showlegend=False, opacity=0.35), row=1, col=1, secondary_y=True)
    max_vol = df["volume"].max() if not df.empty else 1
    fig.update_yaxes(range=[0, max_vol * 5], showgrid=False, secondary_y=True, row=1, col=1)

    if "rsi" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["rsi"], line=dict(width=1.2, color="#ab47bc"), name="RSI"), row=2, col=1)
        fig.add_hline(y=70, line=dict(color="rgba(150,150,150,0.4)", dash="dot", width=1), row=2, col=1)
        fig.add_hline(y=30, line=dict(color="rgba(150,150,150,0.4)", dash="dot", width=1), row=2, col=1)
        fig.update_yaxes(range=[0, 100], row=2, col=1)

    if "macd_hist" in df.columns:
        macd_colors = [_UP if v >= 0 else _DOWN for v in df["macd_hist"].fillna(0)]
        fig.add_trace(go.Bar(x=df.index, y=df["macd_hist"], marker_color=macd_colors, name="MACD", showlegend=False), row=3, col=1)

    if end_labels:
        end_labels.sort(key=lambda x: x["val"])
        min_dist = (df["high"].max() - df["low"].min()) * 0.04
        for i in range(1, len(end_labels)):
            if end_labels[i]["val"] - end_labels[i-1]["val"] < min_dist:
                end_labels[i]["val"] = end_labels[i-1]["val"] + min_dist
                
        for lbl in end_labels:
            fig.add_annotation(
                x=last_x, y=lbl["val"], text=f"— {lbl['name']}",
                showarrow=False, xanchor="left", xshift=10,
                font=dict(color=lbl["color"], size=11, family="JetBrains Mono"),
                row=1, col=1
            )

    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikecolor="rgba(180,180,180,0.6)", spikethickness=1, rangeslider_visible=False)
    fig.update_layout(
        height=850, margin=dict(l=10, r=40, t=30, b=10),
        hovermode="x unified", legend=dict(orientation="h", y=1.02, x=0),
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117"
    )
    return fig

def render_dashboard(result: dict, ticker: str, timeframe: str, fetched_at: float) -> None:
    col_head, col_status = st.columns([1, 1])
    age_s = time.time() - fetched_at
    age_label = f"{int(age_s)}s ago" if age_s < 90 else f"{int(age_s / 60)}m ago"
    
    with col_status:
        rate_tag = "🟢 Live" if result.get("rate_src") == "live Indodax" else "🟡 Fallback"
        data_tag = "🟢 Data clean" if not result.get("data_quality_warnings") else "🔴 Data warning"
        st.markdown(f"<div style='text-align: right; padding-top: 10px; color: #a0a0a0; font-size: 13px;'>{age_label} &nbsp;•&nbsp; {rate_tag} &nbsp;•&nbsp; {data_tag}</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: -15px;'></div>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("##### 🎯 **MTF Alignment**")
        mtf = result.get("mtf", {}) or {}
        layers = mtf.get("layers", {}) or {}
        roles = [("Compass", mtf.get("compass_tf")), ("Engine", mtf.get("engine_tf")), ("Trigger", mtf.get("trigger_tf"))]
        
        c1, c2, c3 = st.columns(3)
        for col, (role, tf) in zip([c1, c2, c3], roles):
            layer = layers.get(tf, {}) or {}
            bias  = layer.get("bias", "unknown").upper()
            adx   = layer.get("adx")
            closes = layer.get("closes", [])
            color = _UP if bias == "BULLISH" else _DOWN if bias == "BEARISH" else _NEUTRAL
            adx_str = f" · ADX {adx:.0f}" if adx else ""
            
            with col:
                c_text, c_spark = st.columns([1.5, 1])
                with c_text:
                    st.caption(f"{role} · {tf or '?'}")
                    st.markdown(f"<span style='color:{color}; font-weight:600;'>{bias}</span><span style='color:#a0a0a0; font-size:13px;'>{adx_str}</span>", unsafe_allow_html=True)
                with c_spark:
                    if closes and len(closes) >= 2:
                        st.plotly_chart(build_sparkline(closes, color), use_container_width=True, config={"displayModeBar": False}, key=f"sp_{role}_{tf}")

    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    col_chart, col_telemetry = st.columns([7, 3], gap="medium")

    with col_chart:
        with st.container(border=True):
            st.markdown(f"##### **{ticker} · {timeframe} Chart**")
            all_trades = load_trades()
            chart_trades = (
                all_trades[(all_trades["ticker"] == ticker) & (all_trades["timeframe"] == timeframe)]
                .to_dict("records")
                if not all_trades.empty else []
            )
            st.plotly_chart(build_chart(result, ticker, timeframe, LAYERS, trades=chart_trades), use_container_width=True)

    with col_telemetry:
        sig       = result.get("signal", {}) or {}
        tr        = result.get("trade_setup", {}) or {}
        ind       = result.get("indicators", {}) or {}
        st_data   = result.get("structure", {}) or {}
        ez        = result.get("entry_zone", {}) or {}
        tradeable = sig.get("tradeable", False)
        score_val = sig.get("score", 0)

        with st.container(border=True):
            st.markdown("##### 🎯 **The Verdict**")
            bias  = sig.get("bias", "")
            sdir  = "+" if isinstance(score_val, int) and score_val > 0 else ""
            sc_color = _UP if isinstance(score_val, int) and score_val > 0 else _DOWN if isinstance(score_val, int) and score_val < 0 else _NEUTRAL
            
            st.markdown(f"**Score:** <span style='color:{sc_color}; font-weight:600;'>{sdir}{score_val} ({bias})</span>", unsafe_allow_html=True)
            st.markdown(f"**Regime:** {result.get('regime', 'N/A')}")
            st.markdown(f"**Confidence:** <span style='color:#ffca28;'>{sig.get('confidence', 'N/A')}</span>", unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown("##### 📊 **Signal Components**")
            comps = sig.get("components", {}) or {}
            
            def fmt_comp(val):
                v = float(val)
                clr = _UP if v > 0 else _DOWN if v < 0 else _NEUTRAL
                return f"<span style='color:{clr}; font-weight:600;'>{v:+.2f}</span>"
            
            c_a, c_b = st.columns(2)
            c_a.caption("Trend")
            c_a.markdown(fmt_comp(comps.get('trend', 0.0)), unsafe_allow_html=True)
            c_a.caption("Momentum")
            c_a.markdown(fmt_comp(comps.get('momentum', 0.0)), unsafe_allow_html=True)
            
            c_b.caption("Volume")
            c_b.markdown(fmt_comp(comps.get('volume', 0.0)), unsafe_allow_html=True)
            c_b.caption("Structure")
            c_b.markdown(fmt_comp(comps.get('structure', 0.0)), unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown("##### 💰 **Execution Matrix**")
            if tr and not tr.get("error") and tradeable:
                st.markdown(f"<div style='font-size:13px; color:#a0a0a0;'>Status</div><div style='color:{_UP}; font-weight:bold; margin-bottom:8px;'>ACTIVE SETUP</div>", unsafe_allow_html=True)
                st.markdown(f"<div style='font-size:13px; color:#a0a0a0;'>Strategy</div><div style='color:{_UP}; margin-bottom:12px;'>{sig.get('strategy', 'ACTIVE')}</div>", unsafe_allow_html=True)
                
                st.markdown(render_telemetry_pill("Entry", tr.get('entry', '?')), unsafe_allow_html=True)
                st.markdown(render_telemetry_pill("Stop Loss", tr.get('sl', '?')), unsafe_allow_html=True)
                st.markdown(render_telemetry_pill("Take Profit", f"{tr.get('tp1', '?')} / {tr.get('tp2', '?')}"), unsafe_allow_html=True)
                st.markdown(render_telemetry_pill("Risk", f"{tr.get('risk_idr', '?')} ({tr.get('pct_pool', '?')} pool)"), unsafe_allow_html=True)

                st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
                if st.button("📌 Log This Trade", key=f"log_trade_{ticker}_{timeframe}", use_container_width=True):
                    if log_trade_open(result, ticker, timeframe, trade_source="MANUAL"):
                        st.success(f"Logged {ticker} entry — auto-tracking TP/SL enabled.")
                        time.sleep(0.75)
                        st.rerun()
                    else:
                        st.error("Could not write to the trade log — check madbot_trades_2.csv isn't locked by another process.")
            else:
                inv_base = sig.get('invalidation') or 'N/A'
                inv_display = f"{inv_base} ({st_data.get('resistance_usd', '')})" if "resistance" in inv_base else inv_base

                if score_val <= -2:
                    status_lbl, status_col = "NO LONG TRADES", _DOWN
                elif score_val >= 1:
                    status_lbl, status_col = "WAITING FOR SETUP", "#ffca28"
                else:
                    status_lbl, status_col = "NEUTRAL / RANGE", _NEUTRAL

                strat_col = _UP if score_val > 0 else _DOWN if score_val < 0 else _NEUTRAL

                st.markdown(f"<div style='font-size:13px; color:#a0a0a0;'>Status</div><div style='color:{status_col}; font-weight:bold; margin-bottom:8px;'>{status_lbl}</div>", unsafe_allow_html=True)
                st.markdown(f"<div style='font-size:13px; color:#a0a0a0;'>Strategy</div><div style='color:{strat_col}; margin-bottom:12px;'>{sig.get('strategy', 'NEUTRAL')}</div>", unsafe_allow_html=True)
                
                ez_bias = ez.get("bias", "")
                if ez_bias == "NO_LONG":
                    watch_lvl = ez.get("watch", {}).get("level", "N/A")
                    st.markdown(render_telemetry_pill("Watch Level", watch_lvl), unsafe_allow_html=True)
                elif ez_bias == "RANGE":
                    st.markdown(render_telemetry_pill("Range Zone", f"{ez.get('low', '?')} – {ez.get('high', '?')}"), unsafe_allow_html=True)
                else:
                    st.markdown(render_telemetry_pill("Entry Note", ez.get('note', 'Wait for edge')), unsafe_allow_html=True)
                    
                st.markdown(render_telemetry_pill("Invalidate", inv_display), unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown("##### 📈 **Current Indicators**")
            
            rate_raw = result.get("rate_raw", 1.0)
            is_global = result.get("is_global", False)

            def format_price(v):
                if not v: return "N/A"
                idr_val = v * rate_raw if is_global else v
                return dual(idr_val, rate_raw)
                
            e21_v = ind.get('ema21')
            vwap_v = ind.get('vwap')
            rsi_v = ind.get('rsi')
            vol_r = result.get('volume', {}).get('ratio', 0)
            
            rsi_display = f"{rsi_v:.1f}" if rsi_v else "N/A"
            
            st.markdown(render_telemetry_pill("EMA 21", format_price(e21_v)), unsafe_allow_html=True)
            st.markdown(render_telemetry_pill("VWAP", format_price(vwap_v)), unsafe_allow_html=True)
            st.markdown(render_telemetry_pill("RSI (14)", rsi_display), unsafe_allow_html=True)
            st.markdown(render_telemetry_pill("Vol Ratio", f"{vol_r:.2f}x"), unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("##### ⚙️ **Engine Diagnostics**")
        d_col1, d_col2 = st.columns(2)
        
        sigs = sig.get("signals", [])
        wrns = sig.get("warnings", [])
        
        with d_col1:
            st.caption("SIGNALS")
            if sigs:
                for s_item in sigs:
                    st.markdown(f"<div style='font-size:14px;'><span style='color:{_UP}; font-weight:bold;'>[+]</span> {s_item}</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div style='font-size:14px; color:#a0a0a0;'>No positive signals recorded.</div>", unsafe_allow_html=True)

        with d_col2:
            st.caption("WARNINGS")
            if wrns:
                for w_item in wrns:
                    st.markdown(f"<div style='font-size:14px;'><span style='color:#ffa726; font-weight:bold;'>[!]</span> {w_item}</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div style='font-size:14px; color:#a0a0a0;'>No warnings logged.</div>", unsafe_allow_html=True)

        with st.expander("View Raw Signal Payload"):
            st.code(json.dumps(sig, indent=2), language="json")

# ── PAGE 1: HOME ─────────────────────────────────────────
if st.session_state.nav_radio == "🏠 Home":
    st.title("Welcome to MadBot OS")
    st.markdown("---")
    st.markdown("#### What would you like to do today?")
    st.markdown("<br>", unsafe_allow_html=True)
    
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.info("📡 **Market Screener**\n\nFind high-probability setups across crypto and stocks based on volume thresholds, ATR ceilings, and momentum.")
        st.button("Open Screener", on_click=switch_page, args=("📡 Screener",), type="primary", use_container_width=True)
    with c2:
        st.success("🎯 **Command Center**\n\nDeep-dive a specific ticker with the full Execution Matrix, S/R zones, and dynamic Chart.")
        st.button("Open Command Center", on_click=switch_page, args=("🎯 Command Center",), type="primary", use_container_width=True)

# ── PAGE 2: SCREENER ────────────────────
elif st.session_state.nav_radio == "📡 Screener":

    st.markdown("<div class='live-badge'>● LIVE</div>", unsafe_allow_html=True)
    st.markdown("<h1>MadBot Market Screener</h1>", unsafe_allow_html=True)
    st.markdown("<div class='subtitle-cyber'>// NEURAL TRADING INTERFACE v2.4.1</div>", unsafe_allow_html=True)
    
    cfg = load_config()
    out = st.session_state.get("screener_results")
    
    if out:
        picks = out.get("picks", [])
        bullish_count = sum(1 for p in picks if p.get("score", 0) > 0)
        total_picks_math = len(picks) if picks else 1 
        total_picks_display = len(picks) 
        avg_vol = sum(p.get("volume_vs_avg", 0) for p in picks) / total_picks_math if picks else 0
        
        rsis = []
        for p in picks:
            for s in p.get("top_signals", []):
                if "RSI" in s:
                    match = re.search(r"RSI ([\d\.]+)", s)
                    if match: rsis.append(float(match.group(1)))
        avg_rsi = sum(rsis) / len(rsis) if rsis else 0.0

        regimes = [p.get("regime", "N/A").replace("TRENDING_", "TREND ").replace("VOLATILE_AVOID", "VOLATILE") for p in picks]
        dom_regime = max(set(regimes), key=regimes.count) if regimes else "N/A"
        
        m_c1, m_c2, m_c3, m_c4 = st.columns(4)
        m_c1.metric("Bullish Tickers", f"{bullish_count} / {total_picks_display}")
        m_c2.metric("Avg RSI", f"{avg_rsi:.1f}")
        m_c3.metric("Avg Vol Ratio", f"{avg_vol:.2f}x")
        m_c4.metric("Dominant Regime", dom_regime)
    else:
        m_c1, m_c2, m_c3, m_c4 = st.columns(4)
        m_c1.metric("Bullish Tickers", "--")
        m_c2.metric("Avg RSI", "--")
        m_c3.metric("Avg Vol Ratio", "--")
        m_c4.metric("Dominant Regime", "--")

    st.markdown("<br>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("<h3 style='font-size:1.1rem; margin-bottom:1rem;'>◈ SCREENER CONFIG</h3>", unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        scn_asset = c1.selectbox("Market", ["crypto", "stock"], index=0)
        scn_tf = c2.selectbox("Scan Timeframe", ["1H", "4H", "1D"], index=0)
        scn_score = c3.number_input("Min Score", value=int(cfg.get("min_score", 2)), step=1)
        scn_vol = c4.number_input("Min Vol Ratio", value=float(cfg.get("min_vol_ratio", 0.8)), step=0.1, format="%.2f")
        
        c5, c6, c7, c8 = st.columns(4)
        scn_atr = c5.number_input("Max ATR %", value=float(cfg.get("max_atr_pct", 10.0)), step=0.5, format="%.1f")
        scn_top = c6.number_input("Top Picks", value=int(cfg.get("top_picks", 5)), step=1)
        scn_dyn = c7.number_input("Universe Size", value=int(cfg.get("crypto_dynamic_top", 20)), step=5)
        
        st.markdown("<br>", unsafe_allow_html=True)
        rc1, rc2 = st.columns([1, 4])
        scn_refresh = rc2.checkbox("Bypass Cache", value=False)
        
        if rc1.button("⚡ RUN SCAN", type="primary", use_container_width=True):
            cfg.update({
                "timeframe": scn_tf,
                "min_score": int(scn_score),
                "min_vol_ratio": float(scn_vol),
                "max_atr_pct": float(scn_atr),
                "top_picks": int(scn_top),
                "crypto_dynamic_top": int(scn_dyn)
            })
            with st.spinner(f"Running neural scan on {scn_asset.upper()}..."):
                start_time = time.time()
                out_data = screen(scn_asset, cfg, force_refresh=scn_refresh, diagnostic=scn_refresh)
                
                if out_data and out_data.get("picks"):
                    trades_df = load_trades()
                    open_sys = trades_df[(trades_df["status"] == "OPEN") & (trades_df["trade_source"] == "SYSTEM")] if not trades_df.empty else pd.DataFrame(columns=_TRADE_COLS)
                    
                    for p in out_data["picks"]:
                        p_ticker = p.get("ticker")
                        p_tf = out_data.get("timeframe", "1H")
                        
                        already_open = open_sys[(open_sys["ticker"] == p_ticker) & (open_sys["timeframe"] == p_tf)]
                        if not already_open.empty:
                            continue
                            
                        try:
                            full_res = analyze(p_ticker, p_tf, asset_type=scn_asset, force_refresh=False, log_signal=False)
                            tr = full_res.get("trade_setup", {})
                            if tr and not tr.get("error") and tr.get("entry_raw_idr"):
                                log_trade_open(full_res, p_ticker, p_tf, trade_source="SYSTEM")
                        except Exception:
                            pass 

                st.session_state.last_scan_duration = time.time() - start_time
                st.session_state.screener_results = out_data
                st.rerun()
                
    if out:
        st.markdown("<br>", unsafe_allow_html=True)
        macro = out.get("macro_context", "NEUTRAL")
        m_color = "#00f0ff" if macro == "BULLISH" else "#ef5350" if macro == "BEARISH" else "#ffca28"
        
        st.markdown(
            f"<div style='font-family:\"Space Mono\", monospace; font-size:0.85rem; color:#8892b0; margin-bottom:15px;'>"
            f"<b>Macro Context:</b> <span style='color:{m_color};'>{macro}</span> "
            f"&nbsp;&nbsp;•&nbsp;&nbsp; Passed: {out.get('total_passed', 0)} / {out.get('total_in_watchlist', 0)} "
            f"&nbsp;&nbsp;•&nbsp;&nbsp; Cached: {out.get('_from_cache', False)}"
            f"</div>", 
            unsafe_allow_html=True
        )
            
        picks = out.get("picks", [])
        if not picks:
            st.info("No tickers passed the current filters.")
        else:
            html_table = "<table class='madbot-table'><thead><tr><th>TICKER</th><th>SCORE</th><th>REGIME</th><th>STRATEGY</th><th>VOL RATIO</th><th>ATR</th><th>SIGNALS</th></tr></thead><tbody>"
            
            for p in picks:
                ticker_val = p.get("ticker", "")
                
                score_val = p.get("score", 0)
                score_color = _UP if score_val > 0 else (_DOWN if score_val < 0 else _NEUTRAL)
                score_html = f"<div class='score-circle' style='color:{score_color}; border-color:{score_color};'>{score_val}</div>"
                
                regime_val = p.get("regime", "").replace("TRENDING_", "TREND ").replace("VOLATILE_AVOID", "VOLATILE")
                reg_color = "#ab47bc" if "SQUEEZE" in regime_val else (_UP if "UP" in regime_val else (_DOWN if "DOWN" in regime_val else "#a0a0a0"))
                regime_html = f"<span style='color:{reg_color}; font-weight:700;'>{regime_val}</span>"

                strat_val = p.get("strategy", "")
                strat_color = _UP if "BULLISH" in strat_val else (_DOWN if "BEARISH" in strat_val else _NEUTRAL)
                strat_html = f"<span style='color:{strat_color}; font-weight:700;'>{strat_val}</span>"

                vol_val = f"{p.get('volume_vs_avg', 0):.2f}x"
                atr_val = f"{p.get('atr_pct', 0):.1f}%"
                
                raw_signals = p.get("top_signals", [])[:4] 
                signals_html = generate_signal_pills(raw_signals)

                html_table += f"<tr><td><b>{ticker_val}</b></td><td>{score_html}</td><td>{regime_html}</td><td>{strat_html}</td><td>{vol_val}</td><td>{atr_val}</td><td>{signals_html}</td></tr>"
            
            html_table += "</tbody></table>"
            st.markdown(html_table, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            with st.container(border=True):
                st.markdown("<h3 style='font-size:1.1rem; margin-bottom:1rem; color:#d946ef !important;'>🚀 Command Center</h3>", unsafe_allow_html=True)
                b_col1, b_col2 = st.columns([3, 1])
                
                ticker_options = [p.get("ticker") for p in picks]
                selected_ticker = b_col1.selectbox("Select Target", ticker_options, label_visibility="collapsed")
                
                b_col2.button(
                    "LAUNCH DEEP DIVE", 
                    type="primary", 
                    use_container_width=True,
                    on_click=launch_in_command_center,
                    args=(selected_ticker, out.get("timeframe", "1H"))
                )

        st.markdown("<br>", unsafe_allow_html=True)
        tab_diag, tab_logs = st.tabs(["Diagnostics", "Logs"])
        
        with tab_diag:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Scan Duration", f"{st.session_state.last_scan_duration:.2f}s")
            c2.metric("API Calls", str(out.get('total_in_watchlist', 0))) 
            c3.metric("Cache Hit", "100%" if out.get('_from_cache') else "0%")
            c4.metric("Errors", str(out.get('total_errors', 0)))
            
            st.markdown("<br>", unsafe_allow_html=True)
            rej = out.get("rejected_detail", [])
            if rej:
                st.markdown("<div class='subtitle-cyber'>RAW FILTER REJECTIONS (--why / diagnostic output)</div>", unsafe_allow_html=True)
                df_rej = pd.DataFrame([{
                    "Ticker": r.get("ticker"),
                    "Score": r.get("score"),
                    "Vol Ratio": f"{r.get('volume_vs_avg', 0):.2f}x",
                    "ATR": f"{r.get('atr_pct', 0):.1f}%",
                    "Reason": r.get("_why_rejected", "Unknown")
                } for r in rej])
                st.dataframe(df_rej, hide_index=True, use_container_width=True)
            else:
                st.info("No diagnostic data. Run scan with 'Bypass Cache' to generate fresh backend rejection data.")
                
        with tab_logs:
            if out.get("macro_warning"):
                st.warning(f"MACRO SYSTEM WARNING: {out['macro_warning']}")
            if out.get("total_errors", 0) > 0:
                st.error(f"SYSTEM FAULT: {out['total_errors']} execution errors logged during scan.")
            else:
                st.success("SYSTEM NOMINAL: No execution errors logged.")

# ── PAGE 3: COMMAND CENTER ─────────────────────────
elif st.session_state.nav_radio == "🎯 Command Center":
    st.title("Command Center")
    
    col_in1, col_in2, col_in3, col_in4, col_in5 = st.columns([2, 1, 1, 1, 1])
    
    with col_in1:
        ticker = st.text_input("Ticker", value=st.session_state.cmd_ticker).strip().upper()
    with col_in2:
        tf_opts = ["1H", "4H", "1D"]
        default_tf = st.session_state.cmd_tf if st.session_state.cmd_tf in tf_opts else "1H"
        timeframe = st.selectbox("Timeframe", tf_opts, index=tf_opts.index(default_tf))
    with col_in3:
        asset_type = st.selectbox("Asset type", ["auto", "crypto", "stock"], index=0)
    with col_in4:
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        force_refresh = st.checkbox("Force Refresh", value=False)
    with col_in5:
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        run_clicked = st.button("Analyze", type="primary", use_container_width=True)

    key = (ticker, timeframe, asset_type, force_refresh)

    open_trades = load_trades()
    open_trades = open_trades[open_trades["status"] == "OPEN"] if not open_trades.empty else open_trades
    with st.expander(f"📋 Active Tracked Trades ({len(open_trades)})", expanded=len(open_trades) > 0):
        if open_trades.empty:
            st.caption("No open positions tracked. Log a setup below to monitor it.")
        else:
            for _, t in open_trades.iterrows():
                try:
                    entry_val = float(t['entry_price_idr'])
                    tp1_val = float(t['tp1_raw_idr'])
                    sl_val = float(t['sl_raw_idr'])
                    status_lbl = f"<span style='color:#00f0ff;'>Targeting {dual(tp1_val)}</span> | <span style='color:#ef5350;'>SL {dual(sl_val)}</span>"
                except:
                    entry_val = 0.0
                    status_lbl = "Awaiting Targets"

                st.markdown(f"**{t['ticker']}** · {t['timeframe']} <span class='signal-pill' style='float:right;'>{t.get('trade_source', 'MANUAL')}</span>", unsafe_allow_html=True)
                st.caption(f"Logged {t['logged_at']} · Entry: {dual(entry_val)} <br> {status_lbl}", unsafe_allow_html=True)
                st.markdown("<hr style='margin: 6px 0; border-color: rgba(255,255,255,0.08);'>", unsafe_allow_html=True)

    if run_clicked or st.session_state.trigger_analysis:
        st.session_state.trigger_analysis = False
        st.session_state.cmd_ticker = ticker
        st.session_state.cmd_tf = timeframe
        
        with st.spinner(f"Analyzing {ticker} {timeframe}..."):
            at = None if asset_type == "auto" else asset_type
            result = analyze(ticker, timeframe, asset_type=at, force_refresh=force_refresh, log_signal=False)
        if "error" in result:
            st.error(f"Analysis error: {result['error']}")
        else:
            st.session_state.analysis_cache[key] = (result, time.time())
            st.session_state.last_key = key
    elif key in st.session_state.analysis_cache:
        st.session_state.last_key = key

    if st.session_state.last_key and st.session_state.last_key in st.session_state.analysis_cache:
        cached_result, fetched_at = st.session_state.analysis_cache[st.session_state.last_key]
        shown_ticker, shown_tf, _, _ = st.session_state.last_key
        render_dashboard(cached_result, shown_ticker, shown_tf, fetched_at)
    elif not run_clicked:
        st.info("Enter a ticker and click Analyze, or run a scan in the Market Screener.")


# ── PAGE 4: LEDGER & PERFORMANCE ─────────────────────────────────────────────
elif st.session_state.nav_radio == "📊 Ledger":
    st.markdown("<div class='live-badge'>● TRACKING</div>", unsafe_allow_html=True)
    st.markdown("<h1>Performance Ledger</h1>", unsafe_allow_html=True)
    st.markdown("<div class='subtitle-cyber'>// MAN VS MACHINE PROOF OF CONCEPT</div>", unsafe_allow_html=True)

    df = load_trades()
    if df.empty:
        st.info("No trades logged yet. Use the Command Center to log a manual pick, or the Screener to generate systemic picks.")
    else:
        man_df = df[df["trade_source"] == "MANUAL"]
        sys_df = df[df["trade_source"] == "SYSTEM"]

        try:
            live_rate, _ = fetch_rate()
        except Exception:
            live_rate = None

        def color_outcome(val):
            v = str(val).upper()
            if 'WIN' in v:
                return 'color: #26a69a; font-weight: bold;'
            elif 'LOSS' in v:
                return 'color: #ef5350; font-weight: bold;'
            elif 'WATCHING' in v:
                return 'color: #ffca28; font-style: italic;'
            return ''
            
        def color_status(val):
            v = str(val).upper()
            if v == 'OPEN':
                return 'color: #00f0ff; font-weight: bold;'
            elif v == 'CLOSED':
                return 'color: #8892b0;'
            return ''

        def render_ledger_table(sub_df, title, color):
            st.markdown(f"<h3 style='color:{color}; font-size:1.2rem;'>{title}</h3>", unsafe_allow_html=True)
            if sub_df.empty:
                st.caption("No trades recorded yet.")
                return
            
            closed = sub_df[sub_df["status"] == "CLOSED"]
            open_cnt = len(sub_df) - len(closed)
            wins = len(closed[closed["exit_reason"].str.contains("WIN", na=False, case=False)])
            win_rate = (wins / len(closed) * 100) if len(closed) > 0 else 0
            
            c1, c2, c3 = st.columns(3)
            c1.metric("Active Tracking", open_cnt)
            c2.metric("Closed Outcomes", len(closed))
            c3.metric("Win Rate", f"{win_rate:.1f}%")
            
            st.markdown("<br>", unsafe_allow_html=True)
            
            display_df = sub_df[["logged_at", "ticker", "timeframe", "entry_price_idr", "status", "exit_reason", "exit_price_idr"]].copy()
            display_df.rename(columns={
                "logged_at": "Time (UTC)", "ticker": "Ticker", "timeframe": "TF",
                "entry_price_idr": "Entry", "status": "Status",
                "exit_reason": "Outcome", "exit_price_idr": "Exit Price"
            }, inplace=True)
            
            formatted_entry = []
            formatted_exit = []
            for _, row in display_df.iterrows():
                try: 
                    ent_val = float(row['Entry'])
                    formatted_entry.append(dual(ent_val, live_rate) if live_rate else dual(ent_val))
                except: 
                    formatted_entry.append("N/A")
                    
                try:
                    ext_val = float(row['Exit Price'])
                    formatted_exit.append(dual(ext_val, live_rate) if live_rate else dual(ext_val))
                except:
                    formatted_exit.append("")
                    
            display_df.loc[display_df["Status"] == "OPEN", "Outcome"] = "Watching..."

            display_df["Entry"] = formatted_entry
            display_df["Exit Price"] = formatted_exit

            # Sort DataFrame FIRST before initializing Pandas Styler to avoid AttributeError
            display_df = display_df.sort_values(by="Time (UTC)", ascending=False)

            styler = display_df.style
            if hasattr(styler, 'map'):
                styled_df = styler.map(color_outcome, subset=['Outcome']).map(color_status, subset=['Status'])
            else:
                styled_df = styler.applymap(color_outcome, subset=['Outcome']).applymap(color_status, subset=['Status'])

            st.dataframe(
                styled_df,
                column_config={
                    "Time (UTC)": st.column_config.TextColumn("Time (UTC)", width="medium"),
                    "Ticker": st.column_config.TextColumn("Ticker", width="small"),
                    "Entry": st.column_config.TextColumn("Entry", width="medium"),
                    "Outcome": st.column_config.TextColumn("Outcome", width="medium"),
                    "Status": st.column_config.TextColumn("Status", width="small")
                },
                hide_index=True,
                use_container_width=True
            )

        col_man, col_sys = st.columns(2, gap="large")
        with col_man:
            render_ledger_table(man_df, "🧑‍💻 MANUAL PICKS", "#00f0ff")
        with col_sys:
            render_ledger_table(sys_df, "🤖 SYSTEM PICKS", "#ab47bc")