# Phase 3 Final Report — Adaptive Tuning Loop

8 iterations 동안 실제 Binance 마켓 데이터로 trade-validator framework를 자가 진단하고, 명확한 패턴에 한해 보수적 튜닝을 적용한 결과 보고서.

---

## TL;DR

- **Tuning #1 적용**: Layer 1 `min_ma_gap_pct=0.001` (0.1%) — ETHUSDT의 noise signal 차단. BTC 같은 명백한 추세는 영향 없음.
- **Framework 정상 동작 검증**: 의도된 use case (retracement-to-swing)에서 5/5 정확히 식별. Breakout/momentum 시나리오는 의도적으로 catch 안 함 (mean-reversion bias).
- **운영 인프라 검증**: Idempotency, dispatcher, state file, scanner synthesizer 모두 라이브에서 정상 작동.
- **권고**: Tuning #1 main에 merge 권장. 추가 튜닝은 1주+ dry-run 후 데이터 모아 결정.

---

## Iteration 데이터 매트릭스

| Iter | 시각 | BTC | ETH | SOL | 이벤트 |
|---|---|---|---|---|---|
| 1 | 01:08 | LONG 3/5 PASS | LONG 4/5 ENTER ⚠️ | skip | Baseline. ETH MA gap 0.007% 의심 |
| 2 | 01:14 | LONG **5/5** ENTER 🎯 | **SHORT** 4/5 (flip!) | LONG 5/5 ENTER | 8분 만에 큰 변화 — retracement-to-swing 시나리오 |
| 3 | 07:06 | LONG 4/5 ENTER | SHORT 4/5 suppressed | skip | Layer 3 ephemeral 신호 입증 |
| 4 | 07:23 | LONG 4/5 suppressed | SHORT 4/5 suppressed | skip | 시장 정체. Phase 1 종료 |
| **5** | **07:40** | **LONG 4/5** (영향 없음) | **SHORT 3/5** (차단됨) | skip | **Tuning #1 적용** — ETH 신호 차단 ✅ |
| 6 | 07:58 | LONG 4/5 suppressed | SHORT 3/5 | skip | Tuning 효과 지속 |
| 7 | 08:15 | LONG **3/5** PASS | skip (no swing) | skip | **BTC +1.64% breakout** — 시장 evolution |
| 8 | 08:32 | LONG 3/5 PASS | skip | skip | Phase 3 전환 |

---

## 적용된 튜닝

### Tuning #1: Layer 1 min MA gap filter

**문제**: ETH의 MA25 vs MA99가 0.005~0.007% 격차로 만나는 상태에서 framework가 LONG/SHORT 양방향에 "4/5 ENTER" 신호를 줌. 8분 사이 방향 flip 관찰 (iter 1 LONG → iter 2 SHORT). 10년차 trader 관점에서 이는 noise이지 추세 아님.

**해결**: `analysis/layers.py` `layer_1_trend()` 에 `min_ma_gap_pct: float = 0.001` (0.1%) 파라미터 추가. 격차가 이 값 미만이면 fail with reason="weak_trend" (방향 무관).

**검증**:
- 3개 단위 테스트 추가 (총 144 tests pass)
- Pre/post 라이브 smoke 비교:
  - BTC (gap 2.26%, 명백한 추세): 4/5 → 4/5 ✅ 무영향
  - ETH (gap 0.005%, noise): SHORT 4/5 → SHORT 3/5 ✅ 차단됨
  - SOL: skip → skip ✅ 무영향

**근거 마진**: 0.1% threshold는 관찰된 noise band(0.005~0.007%)의 ~15배. 명백한 추세(BTC 2.26%, SOL 5.5%)와는 20~50배 차이. 양쪽 모두에서 안전.

---

## 미적용 튜닝 후보 (Future work)

### #2 후보: Scanner direction determination 일관성

`analysis/scanner_logic.py` `determine_direction()`이 Layer 1과 동일한 min_ma_gap 필터를 적용하지 않음. 현재는 weak trend도 direction 결정 후 evaluate_setup에서 Layer 1 fail로 결과. **기능적 영향 없음** (점수는 동일하게 fail) 하지만 코드 일관성 측면에서 개선 가치 있음. Phase 1 dry-run 결과 보고 결정 권장.

### #3 후보: Layer 4 SL buffer 미세 조정

iter 3 BTC에서 SL이 swing low의 정확히 0.30%에 위치 (Layer 4 ±0.5% tolerance 내 안전 마진). 만약 dry-run에서 SL이 너무 자주 stop hit한다면 buffer를 0.4%~0.5%로 증가 검토. **현재 데이터로는 결정 근거 부족.**

### #4 후보: SOL과 같은 변동성 큰 alt에 max_distance 완화

SOL이 iter 1, 3, 4, 6, 7, 8 = 6/8 iterations skipped. 가격이 swing 영역(2%)에서 자주 벗어남. Alt coin의 자연스러운 변동성. 만약 SOL이 framework로 거의 평가 안 되는 게 문제라면 `sl_max_distance_pct`를 alt별로 3%로 증가 검토. **현재는 framework의 보수성 유지가 더 중요 — 미적용.**

### #5 후보: Breakout 시나리오 별도 strategy

iter 7에서 BTC가 swing 위로 +1.64% breakout. Framework는 "PASS" (3/5) — 의도된 mean-reversion bias. Breakout 트레이드는 다른 strategy(예: Donchian channel break, 이전 swing high 돌파 후 retest 등) 필요. **Phase 4+ scope.**

---

## 운영 인프라 검증

라이브 8회 실행에서 다음 모두 정상 작동 확인:

| 컴포넌트 | 검증 사실 |
|---|---|
| `fetch_klines` | Binance public API에서 4h/1h/15m 캔들 fetch, 진행중 캔들 drop |
| `evaluate_setup` | 5개 layer 일관 평가, SetupEvaluation 결과 |
| `synthesize_setup` | entry/SL/TP 자동 생성, 2% 거리 제한 적용 |
| `determine_direction` | Layer 1 mirror, ties → None |
| `AlertState` | JSON state file, ±0.1% swing tolerance, 24h TTL, atomic write |
| `dispatch` | per-channel 격리, 한 채널 실패가 다른 채널 막지 않음 |
| `--quiet` mode | 메타 출력 억제, 에러는 stderr 유지 |
| Suppression | 같은 (symbol, direction, swing) 자동 dedup — iter 3, 4, 6에서 동작 확인 |

---

## 결론 및 권고

1. **Tuning #1을 main에 merge하라**. PR로 별도 분리되어 있어 (branch `adaptive-tuning`) 안전하게 검토 가능. 명백한 framework 개선 + 0 regression + 라이브 검증 완료.

2. **Phase 1 (cron 5분 스캔)을 N주 dry-run하라**. 실제 trade 안 하고, 알림만 받아서 통과율 / 거짓 양성 측정. 그 데이터로 #2~#4 튜닝 후보 결정.

3. **Breakout 트레이딩은 별도 도구로 분리하라**. 현재 5-Layer framework는 mean-reversion에 최적화. Breakout은 다른 패턴 인식이 필요하므로 Phase 4 별도 module.

4. **Trader 룰 보존 우선**: "Score 4/5 미만 = 무조건 패스", "한 주 진입 ≤ 3회", "도구에 100% 의존 X". 이 룰 위반될 변경은 거부.

---

## 부록: 적용 통계

- 총 iteration: 8
- 새 commit: 9 (adaptive-tuning branch)
- 적용 튜닝: 1 (Layer 1 min MA gap filter)
- 추가된 테스트: 3 (144 total pass)
- 식별된 미적용 튜닝 후보: 4
- Real market evolutions 관찰: 2 (retracement, breakout)
