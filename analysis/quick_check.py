"""On-demand symbol check — run the full 5-Layer + S/R snapshot for a single
symbol and return a formatted Telegram-friendly text.

Designed to power `/check SYMBOL` in the Telegram listener. No LLM calls, no
side effects, no idempotency state — just data fetch + scoring.
"""
from __future__ import annotations

from analysis.indicators import add_ma
from analysis.layers import evaluate_setup
from analysis.levels import compute_sr
from analysis.scanner_logic import determine_direction, synthesize_setup
from data.binance import BinanceError, fetch_klines


def quick_check(symbol: str, *, default_rr: float = 3.0) -> str:
    """Return a formatted multi-line summary of the current 5-Layer + S/R state.

    Raises BinanceError for unrecoverable API failures so the caller can decide
    how to surface them. Returns a string in all happy/edge-case paths
    (neutral trend, no setup-able swing, low score, etc.)
    """
    symbol = symbol.upper().strip()
    try:
        df_4h = fetch_klines(symbol, "4h", limit=100)
        df_1h = fetch_klines(symbol, "1h", limit=100)
        df_15m = fetch_klines(symbol, "15m", limit=200)
    except BinanceError as e:
        return f"❌ {symbol}: {e}"

    if df_1h.empty:
        return f"❌ {symbol}: 1H 캔들 데이터 없음"
    current_price = float(df_1h.iloc[-1]["close"])

    df_4h_ma = add_ma(df_4h, [25, 99])
    direction = determine_direction(df_4h_ma)

    sr = compute_sr(df_1h, current_price, n_swing=5, include_ma=True, max_levels=3)
    sr_text = _format_sr(sr, current_price)

    if direction is None:
        return (
            f"📊 {symbol} @ {current_price:,.2f}\n"
            f"   추세: ⏸ 중립 (4H MA25 ≈ MA99 — 진입 보류 권장)\n"
            f"{sr_text}"
        )

    setup = synthesize_setup(df_1h, direction, default_rr=default_rr)
    if setup is None:
        return (
            f"📊 {symbol} @ {current_price:,.2f}\n"
            f"   추세: {direction.upper()} (4H MA25 vs MA99)\n"
            f"   ⚠️ {direction} 방향 구조적 swing이 ±2% 안에 없음 — 진입 보류\n"
            f"{sr_text}"
        )

    ev = evaluate_setup(
        df_4h, df_1h, df_15m,
        entry=setup.entry, sl=setup.sl, tp=setup.tp, direction=direction,
    )
    layer_lines = [
        f"   L{i} {_icon(l.status)} {l.status}"
        for i, l in enumerate(ev.as_layers(), start=1)
    ]
    score_icon = "🟢" if ev.passes else ("⚡" if ev.total_score == 3 else "·")
    l2_d = ev.layer_2.detail
    l2_target = ""
    if l2_d.get("closest_price") and l2_d.get("distance_pct") is not None:
        l2_target = (
            f"\n   L2 가장 가까운 level: {l2_d['closest_label']} @ "
            f"{l2_d['closest_price']:,.2f} ({l2_d['distance_pct']*100:+.2f}%)"
        )

    return (
        f"📊 {symbol} @ {current_price:,.2f}\n"
        f"   추세: {direction.upper()} | {score_icon} 점수 {ev.total_score}/5\n"
        f"   가상 setup: entry={setup.entry:,.2f} SL={setup.sl:,.2f} "
        f"TP={setup.tp:,.2f}\n"
        + "\n".join(layer_lines)
        + l2_target
        + "\n"
        + sr_text
    )


def _icon(status: str) -> str:
    return {"pass": "✅", "fail": "❌", "pending": "⏸"}.get(status, "·")


def _format_sr(sr, current_price: float) -> str:
    """Format support/resistance block. ``sr`` is a SupportResistance instance."""
    lines = ["   ── S/R (1H) ──"]
    if sr.resistance:
        for lbl, p in sr.resistance:
            pct = (p - current_price) / current_price * 100
            lines.append(f"   ▲ R: {lbl} @ {p:,.2f} ({pct:+.2f}%)")
    else:
        lines.append("   ▲ R: (없음 — 가격이 모든 1H swing high 위)")
    if sr.support:
        for lbl, p in sr.support:
            pct = (p - current_price) / current_price * 100
            lines.append(f"   ▼ S: {lbl} @ {p:,.2f} ({pct:+.2f}%)")
    else:
        lines.append("   ▼ S: (없음 — 가격이 모든 1H swing low 아래)")
    return "\n".join(lines)
