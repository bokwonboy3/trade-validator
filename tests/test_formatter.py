from analysis.layers import LayerResult
from output.formatter import ValidationReport, format_report


def _ok(detail=None):
    return LayerResult(score=1, status="pass", detail=detail or {})


def _bad(detail=None):
    return LayerResult(score=0, status="fail", detail=detail or {})


def _pending(detail=None):
    return LayerResult(score=0, status="pending", detail=detail or {})


def _full_pass_report():
    return ValidationReport(
        symbol="BTCUSDT",
        direction="long",
        entry=80_000.0,
        sl=79_700.0,
        tp=80_900.0,
        layer_1=_ok({"ma25": 80_100.0, "ma99": 79_500.0}),
        layer_2=_ok({"closest_label": "swing_low", "closest_price": 79_960.0, "distance_pct": 0.0005}),
        layer_3=_ok({"rejection_volume": 250.0}),
        layer_4=_ok({"passed_swing_price": 79_750.0, "distance_pct": 0.0006}),
        layer_5=_ok({"rr": 3.0, "reward": 900.0, "risk": 300.0, "min_rr": 3.0}),
    )


def test_full_pass_includes_score_and_enter():
    out = format_report(_full_pass_report())
    assert "Score: 5/5" in out
    assert "Recommendation: ENTER" in out


def test_pending_layer3_message():
    r = _full_pass_report()
    r.layer_3 = _pending({"reason": "no_touch_in_history"})
    out = format_report(r)
    assert "PENDING" in out
    assert "Score: 4/5" in out  # still passes 4/5 with other 4 perfect
    assert "Recommendation: ENTER" in out
    # Suggestion mentions re-running
    assert "재실행" in out


def test_fail_layer5_emits_sl_suggestion():
    r = _full_pass_report()
    r.tp = 80_600.0  # R:R = 600/300 = 2.0 — below 3
    r.layer_5 = _bad({"rr": 2.0, "reward": 600.0, "risk": 300.0, "min_rr": 3.0})
    out = format_report(r)
    assert "Score: 4/5" in out
    # New SL = 80_000 - (80_600 - 80_000)/3 = 80_000 - 200 = 79_800
    assert "79,800.00" in out


def test_fail_layer2_suggests_target():
    r = _full_pass_report()
    r.layer_2 = _bad({"closest_label": "swing_low", "closest_price": 80_500.0, "distance_pct": 0.00625})
    out = format_report(r)
    assert "80,500.00" in out
    assert "도달 후 재평가" in out


def test_advisory_hammer_long():
    r = _full_pass_report()
    r.advisory_15m = {"open": 100.0, "high": 100.6, "low": 96.5, "close": 100.5, "volume": 100}
    out = format_report(r)
    assert "Advisory" in out
    assert "망치형 형성 중" in out
