# Adaptive Tuning Loop — Observation + Conservative Tuning

이 문서는 `/loop` 자율 모드로 scan.py를 반복 실행하면서 관찰한 패턴과 적용한 튜닝을 기록합니다. 사용자가 잠들거나 자리를 비운 사이에도 실제 시장 데이터를 누적해서 framework를 자가 진단합니다.

---

## Phase 정의

| Phase | Iterations | 목표 |
|---|---|---|
| 1 | 1~4 | **관찰만** — 데이터 축적, 패턴 식별. 튜닝 금지. |
| 2 | 5~10 | **선별적 튜닝** — 명확한 패턴 1개당 ONE conservative change. pytest + smoke 검증 필수. |
| 3 | last | **최종 보고** — 관찰 요약 + 적용 튜닝 효과 + 미해결 권고. |

## Stop 조건

1. 12 iteration 도달 (~3h)
2. 3 튜닝 적용 (그 이상은 위험)
3. 튜닝 후 pytest 회귀 → 즉시 revert + 종료
4. pytest fail이 같은 곳에서 3회 연속

## 튜닝 우선순위 (10년차 trader 관점)

1. **Layer 1 min MA gap filter** — `min_ma_gap_pct` 추가. MA25/MA99 격차가 이 값 미만이면 Layer 1 fail (reason="weak_trend"). 첫 관찰에서 ETHUSDT가 0.007% 격차로 통과한 케이스를 막아줌. 위험: 매우 낮음 (필터 추가만).
2. **심볼 확장** — config.example.toml에 BNB, AVAX, DOGE 등 추가. 더 많은 데이터 포인트. 위험: 거의 없음.
3. **Threshold 미세 조정** — Layer 2 ±0.3%, Layer 3 vol 1.5x, Layer 4 ±0.5%. 데이터로 명확한 근거가 있을 때만.

## Iteration Cadence

15분 — 15m 캔들 마감과 자연스럽게 정렬, Layer 3 변동을 캡처.

---

## 적용된 튜닝 (Applied Changes)

### Tuning #1 — Layer 1 min MA gap filter (iter 5, 2026-05-11 07:40 KST)

- **변경**: `analysis/layers.py` `layer_1_trend()` 에 `min_ma_gap_pct: float = 0.001` (0.1%) 추가.
  새 모듈 상수 `LAYER1_MIN_MA_GAP_PCT`로 노출.
- **로직**: `abs(ma25 - ma99) / max(|ma25|, |ma99|) < min_ma_gap_pct` 이면 status=fail, reason="weak_trend"
- **테스트**: 3개 추가 (weak trend → fail, clear trend → pass, custom threshold). 144 tests pass.
- **검증 결과** (post-tuning scan vs pre-tuning):
  - BTCUSDT 4/5 → 4/5 (변화 없음, gap 2.26% ≫ 0.1%)
  - **ETHUSDT SHORT 4/5 → SHORT 3/5** (Layer 1 weak_trend로 fail — 의도된 효과)
  - SOLUSDT skipped → skipped (변화 없음)
- **결론**: noise signal 차단 ✅, 명백한 추세는 영향 없음 ✅. 튜닝 성공.

---

## Iteration Log

각 iteration 끝에 한 블록 추가. 형식:

```
### Iteration N [YYYY-MM-DD HH:MM KST]
- BTCUSDT: <score>/5 — <key signal>
- ETHUSDT: ...
- SOLUSDT: ...
- 변화점: <prev iter 대비 무엇이 바뀌었나>
- 패턴 단서: <recurring observation>
- 튜닝 결정: <none | apply X>
```

### Iteration 1 [2026-05-11 01:08 KST]
- BTCUSDT LONG **3/5** PASS — entry 81,418 (current close), Layer 2 fail (swing_high 81,080을 위로 돌파)
- ETHUSDT LONG **4/5** ENTER — entry 2,347.56, MA25 2,318.20 vs MA99 2,318.03 (gap 0.007%), Layer 3 fail
- SOLUSDT — skipped, no qualifying swing within 2%
- 변화점: 첫 관찰 (baseline)
- **패턴 단서 #1**: ETHUSDT의 MA gap이 0.007% — Layer 1 strict 비교(`>`) 통과는 했으나 trader 관점에선 "weak/no trend". 향후 iteration에서 이 패턴이 반복되면 Layer 1 min MA gap filter 후보.
- **패턴 단서 #2**: 통과 케이스에서 Layer 3는 항상 fail — "지금은 진입 시점 아님" 일관 신호.
- 튜닝 결정: none (Phase 1 관찰만)

### Iteration 2 [2026-05-11 01:14 KST] (+6분)
- BTCUSDT LONG **5/5** ENTER 🟢 — entry 80,734.88 (8분 전 81,418에서 −0.84% retraced!), 모든 5 layers PASS. Layer 2 swing_low 80,725 (0.01%), Layer 3 ✅ 거부 캔들 + volume 150.09, Layer 4 SL @ 80,482.91 (swing low 0.11%)
- ETHUSDT **SHORT** 4/5 ENTER 🟢 — entry 2,328.76, **방향 LONG → SHORT FLIP!** MA25 2,318.46 < MA99 2,318.58 (gap 0.005%, 직전엔 +0.007%였음). Layer 3 ❌
- SOLUSDT LONG **5/5** ENTER 🟢 — entry 94.52, 8분 전 skipped → 가격이 swing 범위 내로 회복. Layer 2 swing_high 94.80 (0.30%), Layer 3 ✅ volume 119,018
- 변화점:
  - BTC: retracement으로 swing_low에 정확히 도달 → 5/5 (이게 framework가 노리는 시나리오)
  - ETH: MA gap 0.005~0.007% 사이에서 진동 → 8분 만에 LONG↔SHORT 뒤집힘
  - SOL: out-of-range → in-range 전환
- **패턴 단서 #1 강화**: ETH의 MA gap이 0.005~0.007% 범위에서 LONG/SHORT가 뒤집힘. 이는 framework의 핵심 약점 — **noise를 trend로 분류**. 한 번 더 0.1% 이하 gap 관찰되면 Phase 2에서 즉시 튜닝 후보.
- **패턴 단서 #3 (신규)**: BTC가 swing_low에서 거부 캔들 + 거래량 spike → 모든 5 layers passing. 이는 framework가 의도한 "이상적 셋업" 신호. Real trade라면 진입 고려 가치.
- 튜닝 결정: none (Phase 1, iter 2/4)

### Iteration 3 [2026-05-11 07:06 KST] (+15분 from iter 2)
- BTCUSDT LONG **4/5** ENTER 🟢 — entry 80,720 (iter 2: 80,734, −0.02%), Layer 3 **✅→❌** (8분 전 거부 캔들이 더이상 "recent" 아님), Layer 2 swing_low 80,725.09 (0.01%), Layer 4 SL 80,331.05 (swing 80,572.77 기반 — entry가 직전 swing low 아래로 내려가서 synthesizer가 더 깊은 swing 사용)
- ETHUSDT SHORT 4/5 — **suppressed (이미 24h 내 알림)** — Layer 1 약세 유지 (15분간 flip 없음). Idempotency 정상 동작 확인.
- SOLUSDT — skipped (다시 range 밖)
- 변화점:
  - BTC Layer 3 ephemeral 신호: 5/5 → 4/5 (15min). 거부 캔들 신호의 lifespan은 ~15분 (다음 15m 마감 시 사라짐). **진입 윈도우 짧다는 framework 특성.**
  - ETH 15분간 SHORT 유지 — iter 1→2의 flip 후 안정. MA gap이 strict 양수로 자리잡았을 가능성.
  - SOL: range 안↔밖 진동 (높은 변동성)
- **Idempotency 검증**: ETH 같은 (symbol, direction, swing) signature → 자동 suppress. 핵심 기능 OK.
- **Synthesizer 적응성 확인**: BTC entry가 직전 swing 아래로 떨어지자 더 깊은 swing 자동 선택. 같은 셋업이 아니라 새 셋업으로 인식 (정답).
- 튜닝 결정: none (Phase 1, iter 3/4)

### Iteration 4 [2026-05-11 07:23 KST] (+17분 from iter 3)
- BTCUSDT LONG 4/5 ENTER — **iter 3과 정확히 동일** (entry 80,720.47, SL 80,331.05, TP 81,888.72)
- ETHUSDT SHORT 4/5 ENTER — **iter 3과 동일**
- SOLUSDT skipped — 동일
- **둘 다 suppressed** → 0 dispatch
- 변화점: **시장 정체**. 07:00→08:00 1H 캔들 미마감으로 1H 기반 layers (1, 2, 4)는 동일. 새 15m 캔들 닫혔으나 Layer 3에 영향 줄 거부 패턴 없음.
- **Phase 1 종료**. 4회 관찰 누적.

## Phase 1 종합 (iter 1~4)

| 관찰 | 횟수 | 결론 |
|---|---|---|
| ETH MA gap < 0.01% | 2회 (iter 1 LONG, iter 2 SHORT flip) | **튜닝 우선순위 #1** — 0.005~0.007% gap에서 방향 flip 발생, framework 신호 신뢰도 낮음 |
| BTC retracement-to-swing 5/5 | 1회 (iter 2) | Framework가 의도한 시나리오 정상 작동 |
| Layer 3 ephemeral lifespan | 1회 (iter 2→3, 5/5→4/5) | 거부 캔들 신호 ~15분 단명 |
| SOL range 진동 | 2회 (in→out, out→in→out) | 더 변동성 큰 alt — Layer 2 ±0.3% 너무 빡빡할 가능성 (관찰 부족, 보류) |
| Idempotency | iter 3, 4 | 정상 동작 — 같은 signature 자동 suppress |

### Iteration 5 [2026-05-11 07:40 KST] (Phase 2 시작, Tuning #1)

**Pre-tuning baseline**: BTCUSDT 4/5, ETHUSDT SHORT 4/5, SOLUSDT skip (iter 3/4와 동일)

**Tuning applied**: Layer 1 `min_ma_gap_pct=0.001` (0.1%) — 상세는 위 "적용된 튜닝" 섹션

**Post-tuning scan** (state file reset for clean comparison):
- 🟢 BTCUSDT LONG 4/5 ENTER (변화 없음 — gap 2.26%, 필터 영향 0)
- · ETHUSDT SHORT **3/5 PASS** (Layer 1 fail, reason="weak_trend" — 의도대로 차단)
- ⏭ SOLUSDT skipped (변화 없음)

**효과**:
- ✅ ETH noise signal (4/5 → 3/5) 차단 — trader 보수주의 강화
- ✅ BTC 명백한 추세 (gap 2.26%) 영향 없음
- ✅ 144 tests pass (test_layer_1.py에 3개 추가)
- ✅ pytest + smoke 검증 통과 → commit/push

**튜닝 결정**: applied (1/3 max). iter 6+에서 추가 패턴 관찰. 새 튜닝 후보 발견되면 적용.
