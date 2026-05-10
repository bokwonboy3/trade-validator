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

(아직 없음 — Phase 1)

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
