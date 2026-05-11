"""Result formatting for the CLI output."""
from __future__ import annotations

from dataclasses import dataclass

from analysis.candles import Candle, is_hammer, is_shooting_star
from analysis.layers import LayerResult


@dataclass
class ValidationReport:
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    layer_1: LayerResult
    layer_2: LayerResult
    layer_3: LayerResult
    layer_4: LayerResult
    layer_5: LayerResult
    advisory_15m: Candle | None = None  # in-progress 15m advisory, None if no live candle
    # Agentic tier output. None when API key absent or analysis skipped.
    # Type is `Any` to avoid circular dependency with agents.types.
    agent_verdict: object | None = None
    # Per-specialist outputs (also `Any` to avoid circular dep).
    agent_specialists: list[object] | None = None
    # Alert ID for cross-referencing in journal CLI (None for validate.py path)
    alert_id: str | None = None

    @property
    def total_score(self) -> int:
        return (
            self.layer_1.score
            + self.layer_2.score
            + self.layer_3.score
            + self.layer_4.score
            + self.layer_5.score
        )

    @property
    def passes(self) -> bool:
        return self.total_score >= 4


_EMOJI_GLYPHS = {
    "pass": "✅",
    "fail": "❌",
    "pending": "⏸",
    "header": "📊",
    "score": "🎯",
    "enter": "🟢",
    "watch": "🟡",
    "block": "🚫",
    "info": "ℹ",
    "tip": "💡",
}
_PLAIN_GLYPHS = {
    "pass": "[PASS]",
    "fail": "[FAIL]",
    "pending": "[PEND]",
    "header": "[5L]",
    "score": "[Score]",
    "enter": "[ENTER]",
    "watch": "[WATCH]",
    "block": "[PASS]",
    "info": "[INFO]",
    "tip": "[TIP]",
}


def _glyph(key: str, *, no_emoji: bool) -> str:
    return _PLAIN_GLYPHS[key] if no_emoji else _EMOJI_GLYPHS[key]


def _icon(status: str, *, no_emoji: bool = False) -> str:
    return _glyph(status, no_emoji=no_emoji)


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def _recommendation(r: "ValidationReport") -> tuple[str, str, str]:
    """Return (label, flag_glyph_key, advisory_note) based on score + which layer failed.

    The 5-Layer framework's threshold is 4/5, but a 4/5 score isn't monolithic —
    *which* layer failed changes what the user should do. Layer 3 (trigger) is
    especially important: 4/5 with Layer 3 ❌ means "level reached but no
    rejection signal" → MONITOR, not ENTER.
    """
    score = r.total_score
    if score < 4:
        return ("PASS", "block", "")
    if score == 5:
        return ("ENTER", "enter", "5개 layer 모두 정렬 — 강한 신호")

    # score == 4: identify which layer failed/pending
    l3 = r.layer_3.status
    if l3 == "pass":
        # Some other layer failed. Layer 3 (the trigger) IS confirmed.
        return ("ENTER", "enter", "Layer 3 거부 신호 확정 — 진입 고려")
    if l3 == "pending":
        return (
            "PLAN OK",
            "watch",
            "entry 근처 미방문 — 도달 시 도구 재실행으로 Layer 3 평가",
        )
    # Layer 3 fail (most common 4/5): level reached but no rejection candle
    # Other 4 layers passing means the SETUP is structurally sound, but the
    # market hasn't shown commitment at this level yet.
    return (
        "WATCH",
        "watch",
        "level 도달했으나 거부 신호 없음 — 진입 X, FORMING 또는 5/5 업그레이드 대기",
    )


def format_report(r: ValidationReport, *, no_emoji: bool = False) -> str:
    lines: list[str] = []
    lines.append("=== Setup Validation ===")
    if r.alert_id:
        lines.append(f"Alert ID: {r.alert_id}")
    lines.append(f"Symbol: {r.symbol}")
    lines.append(f"Direction: {r.direction.upper()}")
    lines.append(
        f"Entry: {_fmt_money(r.entry)} / SL: {_fmt_money(r.sl)} / TP: {_fmt_money(r.tp)}"
    )
    lines.append("")
    lines.append(f"{_glyph('header', no_emoji=no_emoji)} 5-Layer Evaluation:")

    def ic(status: str) -> str:
        return _icon(status, no_emoji=no_emoji)

    # Layer 1
    d1 = r.layer_1.detail
    if r.layer_1.status == "pass":
        align = "강세 정렬" if r.direction == "long" else "약세 정렬"
        lines.append(
            f"{ic(r.layer_1.status)} Layer 1: 4H {align} "
            f"(MA25 {_fmt_money(d1['ma25'])} {'>' if r.direction=='long' else '<'} MA99 {_fmt_money(d1['ma99'])})"
        )
    else:
        lines.append(
            f"{ic(r.layer_1.status)} Layer 1: 4H 정렬 미충족 "
            f"(MA25 {_fmt_money(d1['ma25'])}, MA99 {_fmt_money(d1['ma99'])})"
        )

    # Layer 2
    d2 = r.layer_2.detail
    if r.layer_2.status == "pass":
        lines.append(
            f"{ic(r.layer_2.status)} Layer 2: 핵심 레벨 근처 "
            f"({d2['closest_label']} {_fmt_money(d2['closest_price'])}, 거리 {d2['distance_pct']*100:.2f}%)"
        )
    elif d2.get("reason") == "no_levels_found":
        lines.append(
            f"{ic(r.layer_2.status)} Layer 2: 핵심 레벨 미발견 (1H 데이터 부족)"
        )
    else:
        lines.append(
            f"{ic(r.layer_2.status)} Layer 2: 핵심 레벨 아님 "
            f"(가장 가까운: {d2['closest_label']} {_fmt_money(d2['closest_price'])}, 거리 {d2['distance_pct']*100:.2f}%)"
        )

    # Layer 3
    d3 = r.layer_3.detail
    if r.layer_3.status == "pass":
        lines.append(
            f"{ic(r.layer_3.status)} Layer 3: 거부 캔들 + 거래량 확인 (volume {_fmt_money(d3['rejection_volume'])})"
        )
    elif r.layer_3.status == "pending":
        reason = d3.get("reason", "")
        msg = (
            "entry 근처 50h 내 미방문 — 도달 시 재실행 권장"
            if reason == "no_touch_in_history"
            else "터치 시점의 15m 데이터 부족"
        )
        lines.append(f"{ic(r.layer_3.status)} Layer 3: PENDING — {msg}")
    else:
        reason = d3.get("reason", "")
        msg = "거부 캔들 + 거래량 미충족" if reason else "조건 미충족"
        lines.append(f"{ic(r.layer_3.status)} Layer 3: {msg}")

    # Layer 4
    d4 = r.layer_4.detail
    if r.layer_4.status == "pass":
        lines.append(
            f"{ic(r.layer_4.status)} Layer 4: SL이 swing {'low' if r.direction=='long' else 'high'} "
            f"({_fmt_money(d4['passed_swing_price'])}) 근처 ({d4['distance_pct']*100:.2f}%)"
        )
    else:
        if d4.get("closest_swing_price") is not None:
            lines.append(
                f"{ic(r.layer_4.status)} Layer 4: SL이 구조 밖 "
                f"(가장 가까운 swing {'low' if r.direction=='long' else 'high'} {_fmt_money(d4['closest_swing_price'])}, "
                f"거리 {d4['distance_pct']*100:.2f}%)"
            )
        else:
            lines.append(
                f"{ic(r.layer_4.status)} Layer 4: 구조 swing 미발견"
            )

    # Layer 5
    d5 = r.layer_5.detail
    rr_str = f"R:R {d5['rr']:.2f}"
    if r.layer_5.status == "pass":
        lines.append(f"{ic(r.layer_5.status)} Layer 5: {rr_str} (≥ {d5['min_rr']})")
    else:
        lines.append(f"{ic(r.layer_5.status)} Layer 5: {rr_str} ({d5['min_rr']} 미달)")

    lines.append("")
    lines.append(f"{_glyph('score', no_emoji=no_emoji)} Score: {r.total_score}/5")
    rec_label, rec_flag, rec_note = _recommendation(r)
    flag = _glyph(rec_flag, no_emoji=no_emoji)
    lines.append(f"{flag} Recommendation: {rec_label}")
    if rec_note:
        lines.append(f"   → {rec_note}")

    # Suggestions for failed layers
    suggestions = _build_suggestions(r)
    if suggestions:
        lines.append("")
        lines.append(f"{_glyph('tip', no_emoji=no_emoji)} Suggestions:")
        lines.extend(f"- {s}" for s in suggestions)

    # Agent verdict (Tier 2~4 agentic analysis)
    if r.agent_verdict is not None:
        av = r.agent_verdict
        # Use duck-typing to avoid agents import (keeps formatter standalone)
        a_verdict = getattr(av, "verdict", None)
        a_conf = getattr(av, "confidence", None)
        a_rationale = getattr(av, "rationale", "")
        a_downgraded = getattr(av, "downgraded_from_tier1", False)
        a_tier1 = getattr(av, "tier1_verdict", None)
        if a_verdict is not None:
            lines.append("")
            # Per-specialist findings (when available)
            if r.agent_specialists:
                lines.append("🤖 Specialist Findings:")
                for s in r.agent_specialists:
                    line = _format_specialist_line(s, no_emoji=no_emoji)
                    if line:
                        lines.append(f"  {line}")
                lines.append("")
            arrow = f" (Tier 1: {a_tier1} → {a_verdict})" if a_downgraded else ""
            lines.append(
                f"🤖 Final Verdict: {a_verdict}{arrow}  (confidence {a_conf}%)"
            )
            if a_rationale:
                lines.append(f"   → {a_rationale}")

    # Advisory: in-progress 15m candle
    if r.advisory_15m is not None:
        adv = _build_advisory(r.advisory_15m, r.direction)
        if adv:
            lines.append("")
            lines.append(f"{_glyph('info', no_emoji=no_emoji)} Advisory (진행중 15m, 점수 무영향):")
            lines.extend(f"  {a}" for a in adv)

    return "\n".join(lines)


def _build_suggestions(r: ValidationReport) -> list[str]:
    out: list[str] = []
    if r.layer_2.status == "fail":
        d = r.layer_2.detail
        out.append(
            f"Layer 2: {_fmt_money(d['closest_price'])} 도달 후 재평가 권장"
        )
    if r.layer_5.status == "fail":
        d5 = r.layer_5.detail
        if r.direction == "long":
            # Suggest a tighter SL that yields R:R = min_rr with same TP
            new_sl = r.entry - (r.tp - r.entry) / d5["min_rr"]
            out.append(
                f"Layer 5: TP 동일 시 SL을 {_fmt_money(new_sl)}로 이동하면 R:R {d5['min_rr']:.1f}"
            )
        else:
            new_sl = r.entry + (r.entry - r.tp) / d5["min_rr"]
            out.append(
                f"Layer 5: TP 동일 시 SL을 {_fmt_money(new_sl)}로 이동하면 R:R {d5['min_rr']:.1f}"
            )
    if r.layer_3.status == "pending":
        out.append(
            "Layer 3: entry 도달 후 도구 재실행 — 거부 캔들 평가 가능해짐"
        )
    return out


def _format_specialist_line(spec: object, *, no_emoji: bool = False) -> str:
    """One-line summary of a specialist's output for inline alert display.

    Uses duck-typing on SpecialistOutput so formatter stays decoupled from
    agents package. Returns "" when the input doesn't look like a spec output.
    """
    name = getattr(spec, "name", None)
    failed = getattr(spec, "failed", False)
    if name is None:
        return ""

    if failed:
        reason = getattr(spec, "failure_reason", "")[:80]
        mark = "[FAIL]" if no_emoji else "⚠"
        return f"{mark} {name}: failed ({reason})"

    findings = getattr(spec, "findings", {}) or {}
    confidence = getattr(spec, "confidence", 0)
    rationale = getattr(spec, "rationale", "")[:100]

    # Pick the most informative key per specialist (best-effort)
    primary = ""
    if name == "microstructure":
        rq = findings.get("rejection_quality", "?")
        primary = f"rejection={rq}"
    elif name == "trend_context":
        ts = findings.get("trend_strength", "?")
        tq = findings.get("trend_quality", "?")
        primary = f"strength={ts}/10 quality={tq}"
    elif name == "volume_regime":
        regime = findings.get("regime", "?")
        primary = f"regime={regime}"
    elif name == "risk":
        slq = findings.get("sl_quality", "?")
        tpr = findings.get("tp_realism", "?")
        hold = findings.get("expected_hold_hours")
        primary = f"sl={slq} tp={tpr} hold={hold}h"
    elif name == "macro":
        bias = findings.get("macro_bias", "?")
        events = findings.get("unusual_events") or []
        events_str = f" events={len(events)}" if events else ""
        primary = f"bias={bias}{events_str}"
    else:
        primary = ", ".join(f"{k}={v}" for k, v in list(findings.items())[:2])

    rationale_part = f" — {rationale}" if rationale else ""
    return f"· {name} (conf {confidence}/10) {primary}{rationale_part}"


def _build_advisory(candle: Candle, direction: str) -> list[str]:
    """Describe an in-progress 15m candle (no scoring impact)."""
    out: list[str] = []
    forming_hammer = is_hammer(candle)
    forming_star = is_shooting_star(candle)
    if direction == "long" and forming_hammer:
        out.append("진행중 15m: 망치형 형성 중 (LONG 신호 가능) — 마감 시 재확인")
    elif direction == "short" and forming_star:
        out.append("진행중 15m: 슈팅스타 형성 중 (SHORT 신호 가능) — 마감 시 재확인")
    elif forming_hammer:
        out.append("진행중 15m: 망치형 형성 중 (반대 방향)")
    elif forming_star:
        out.append("진행중 15m: 슈팅스타 형성 중 (반대 방향)")
    else:
        out.append("진행중 15m: 거부 패턴 미형성")
    return out
