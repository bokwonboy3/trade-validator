# Trade Validator — 전체 워크플로우 정리

## 1. 무엇인가

BTC perpetual futures **trading setup을 자동 평가 + 알람**하는 도구.
사용자의 5-Layer 트레이딩 framework를 코드화하고, 그 위에 LLM 기반 multi-agent 분석을 얹어 Telegram으로 알람.

**핵심 목적**: 감정적/일관성 결여 트레이드를 막는 *discipline 장치*.

---

## 2. 시스템 계층 (Tier)

```
┌────────────────────────────────────────────────┐
│ Tier 4: Action Recommender (rule-based)         │
│   downgrade-only enforcement                     │
└──────────────▲──────────────────────────────────┘
               │
┌──────────────┴──────────────────────────────────┐
│ Tier 3: Meta-Judge                              │
│   cross-validation, hallucination risk          │
└──────────────▲──────────────────────────────────┘
               │
┌──────────────┴──────────────────────────────────┐
│ Tier 2: 5 Specialists (parallel LLM calls)      │
│   - Microstructure (캔들 형태)                  │
│   - Trend Context (multi-timeframe MA)          │
│   - Volume Regime (accumulation/distribution)   │
│   - Risk (ATR-aware SL/TP)                      │
│   - Macro (funding rate + OI)                   │
└──────────────▲──────────────────────────────────┘
               │ trigger if score >= 4 OR forming
┌──────────────┴──────────────────────────────────┐
│ Tier 1: Deterministic 5-Layer (PURE Python)     │
│   - Layer 1: 4H MA 추세 정합성 (+ min gap 0.1%)  │
│   - Layer 2: 1H swing/MA 핵심 레벨 ±0.3%        │
│   - Layer 3: 15m 거부 캔들 + 거래량 (Hybrid)    │
│   - Layer 4: SL이 swing 너머 단방향 ±0.5%       │
│   - Layer 5: R:R ≥ 3.0                          │
└──────────────▲──────────────────────────────────┘
               │
┌──────────────┴──────────────────────────────────┐
│ Data: Binance Public + Futures API              │
│   klines (4h/1h/15m/1m), funding rate, OI       │
└─────────────────────────────────────────────────┘
```

**핵심 룰**: Tier 1은 신성불가침. Agent는 verdict를 **downgrade만 가능, upgrade 절대 불가**.

---

## 3. 1분간의 cron tick 흐름

```
00:00 cron 실행
  ↓ . ./.tv-env (Telegram, Anthropic env, PATH 로드)
  ↓ python scan.py --quiet
  ↓
[scan.py] for each symbol (BTC/ETH/SOL):
  1. fetch_klines() 4h/1h/15m/1m  ← Binance API
  2. determine_direction()  ← 4H MA25 vs MA99
  3. synthesize_setup()  ← entry/SL/TP 자동 생성
  4. evaluate_setup()  ← 5 layers 평가
  5. detect_forming_rejection()  ← 진행중 15m forming?
  6. if score >= 4 or forming:
        run_agentic_analysis()  ← 5 specialists 병렬 LLM 호출
          → meta_judge → recommender (downgrade-only)
          → record to market_memory.jsonl
  7. Build ValidationReport
  ↓
[idempotency] AlertState.already_alerted()?
  ↓ 새 셋업이면
[dispatch] build_channels(stdout, file, telegram)
  ↓
  - print to stdout (cron log)
  - append to alerts.log + alerts.jsonl
  - POST to Telegram API
  ↓
[state] state.save() — 24h dedup
```

평균 50~60초/tick (5 LLM 호출 병렬 + 데이터 fetch).

---

## 4. 현재 가동 상태

| 항목 | 값 |
|---|---|
| **Cron** | `*/2 * * * *` (2분 간격) |
| **Symbols** | BTCUSDT, ETHUSDT, SOLUSDT |
| **Threshold** | 4/5 (dispatch + agent trigger) |
| **Agent backend** | `claude` CLI (plan quota) |
| **Agent model** | Sonnet 4.6 (default) |
| **Notification channels** | stdout + file + telegram |

---

## 5. 파일 구조

```
trade-validator/
├── main code
│   ├── validate.py            CLI 단일 setup 검증
│   ├── scan.py                Cron 자동 스캐너
│   ├── scanner_config.py      config.toml 로더
│   ├── alert_state.py         idempotency (24h TTL)
│   ├── analysis/
│   │   ├── indicators.py      MA, ATR
│   │   ├── levels.py          swing high/low (N=5)
│   │   ├── candles.py         hammer/star + volume spike
│   │   ├── layers.py          5 Layer 함수 + evaluate_setup
│   │   ├── scanner_logic.py   direction + synthesize SL/TP
│   │   └── forming.py         진행중 15m FORMING 감지
│   ├── agents/                 ← Tier 2~4
│   │   ├── types.py           SpecialistOutput, AgentVerdict
│   │   ├── client.py          Anthropic API client
│   │   ├── cli_client.py      claude CLI client (plan quota)
│   │   ├── backend.py         CLI/API 선택 로직
│   │   ├── specialist_base.py 공통 invocation
│   │   ├── microstructure.py  특화: 캔들 nuance
│   │   ├── trend_context.py   특화: multi-timeframe MA
│   │   ├── volume_regime.py   특화: accumulation/distribution
│   │   ├── risk.py            특화: ATR-aware SL/TP
│   │   ├── macro.py           특화: funding + OI
│   │   ├── meta_judge.py      cross-validation
│   │   ├── recommender.py     downgrade-only enforcement
│   │   ├── memory.py          market_memory.jsonl 관리
│   │   └── runner.py          오케스트레이션 (parallel)
│   ├── data/binance.py        klines + funding + OI fetch
│   └── output/
│       ├── formatter.py       alert 포맷
│       └── notify.py          stdout/file/telegram dispatch
├── config / runtime (모두 gitignored)
│   ├── config.toml            본인 설정
│   ├── .tv-env                토큰 + PATH
│   ├── .tv-state.json         idempotency state
│   ├── alerts.log             텍스트 알람 누적
│   ├── alerts.jsonl           구조화 알람 누적
│   ├── market_memory.jsonl    agent 메모리
│   └── scan.log               cron 로그
└── tests/                     215 tests (212 offline + 7 live + 4 snapshots)
    ├── snapshots/             agent 회기 baseline
    └── fixtures/              Binance JSON 캡쳐
```

---

## 6. PR 히스토리 (10개 merged)

| # | 무엇 |
|---|---|
| 1 | Phase A (baseline) + Phase B (Phase 1 scanner) |
| 3 | Tuning #1: Layer 1 min MA gap filter |
| 4 | Production hardening: live tests, cron env, state stress |
| 5 | FORMING tier (intra-candle 실시간 신호) |
| 6 | Nuanced recommendation (4/5 L3 fail → WATCH) |
| 7 | Agentic v1: 5 specialists + meta + recommender + CLI backend |
| 8 | Agentic Phase 2: 4 stubs → 실 LLM specialist (병렬) |
| 9 | Diagnostic logging (specialist failure_reason stderr) |
| 10 | Phase 3+: specialist findings 상세 / market memory / snapshots / jsonl / stats |

---

## 7. Trader 룰 (불변)

| 룰 | 의미 |
|---|---|
| Score 4/5 미만 = 무조건 패스 | Tier 1 보수성 |
| 한 주 진입 ≤ 3회 | 사용자 self-discipline |
| 도구에 100% 의존 X | 최종 판단은 본인 |
| 진입 후 plan stick | 도구 결과로 SL/TP 변경 금지 |

**Agent도 이 룰을 위반 못 함** (downgrade-only invariant).

---

## 8. Verdict 해석

| 표시 | 의미 | 행동 |
|---|---|---|
| 🟢 ENTER (5/5) | 5개 layer 모두 정렬 | 진입 검토 |
| 🟢 ENTER (4/5, L3 ✅) | 거부 신호 확정 | 진입 검토 |
| 🟡 PLAN OK | 4/5 + L3 pending (fresh level) | entry 도달 시 재실행 |
| 🟡 WATCH | 4/5 + L3 ❌ (거부 없음) | **진입 X**, 5/5 또는 FORMING 대기 |
| ⚡ FORMING | 진행중 15m에 거부 형성 중 | 15m 마감 후 재확인 |
| 🚫 PASS | <4/5 | 무시 |
| 🤖 Tier1→PASS (downgrade) | Agent가 추가 concern 발견 | 진입 X |

---

## 9. 데이터 출처

- **Klines** — Binance public spot API (auth 불필요)
- **Funding rate / OI** — Binance perp futures (auth 불필요)
- **LLM** — Claude Sonnet 4.6 via `claude` CLI (plan quota 차감)
- **Telegram** — Bot API (BotFather 토큰)

---

## 10. 알려진 한계

- Mac sleep → cron 중단 (노트북 닫으면 알람 안 옴)
- WebSocket tick-level 신호 X (1초 spike 못 잡음)
- Phase 2 journal 없음 (실제 트레이드 결과 추적 X)
- Snapshot tests는 mock 기반 (실 LLM drift는 manual 검증)

---

## 11. 모니터링 명령어

```bash
cd /Users/bokwon/trade-validator

# 최근 cron 실행 로그
tail -20 scan.log

# 모든 dispatched 알람
cat alerts.log

# 구조화된 알람 (분석용)
cat alerts.jsonl | tail -10

# 시장 메모리 (agent 컨텍스트)
cat market_memory.jsonl | tail -10

# 최근 24h 통계
.venv/bin/python scan.py --stats

# 7일치 통계
.venv/bin/python scan.py --stats --stats-hours 168

# Cron 확인
crontab -l

# 직접 테스트 실행 (state 안 건드림)
. ./.tv-env && .venv/bin/python scan.py --config config.toml --state /tmp/test.json
```

---

## 12. 다음 결정 포인트

| 시점 | 무엇 |
|---|---|
| 며칠 후 | `--stats` 결과로 framework 가치 평가 |
| 통과율 < 5% | swing N 또는 vol multiplier 완화 |
| 통과율 > 30% | min_score 5로 상향 |
| Agent confidence 일관 낮음 | specialist prompt 튜닝 |
| Specialist 자주 실패 | scan.log 봐서 원인 식별 |
| 실거래 시작 | Phase 4: trade journal (SQLite) |
| Mac sleep 문제 심함 | VPS 이주 |
| 실시간 1초 신호 필요 | Phase 5: WebSocket stream |

---

## 한 줄 요약

**Mac이 깨어있는 동안 매 2분, 3개 심볼에 대해 5-Layer + 5-agent 분석을 자동 수행 → 의미 있는 셋업 발견 시 휴대폰으로 텔레그램 알람 → 사용자가 차트 확인 후 진입 여부 결정.**
