# Trade Validator (Phase 0 MVP)

BTC perpetual futures 셋업을 5-Layer 프레임워크로 자동 평가하는 CLI 도구.

---

## Installation

```bash
git clone https://github.com/bokwonboy3/trade-validator.git
cd trade-validator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

요구사항: **Python 3.10+**, 인터넷 연결 (Binance public API). API 키는 불필요.

---

## Usage

### 기본 호출

```bash
python validate.py \
  --symbol BTCUSDT \
  --entry 80841 \
  --sl 80500 \
  --tp 81700 \
  --direction long
```

| 인자 | 의미 |
|---|---|
| `--symbol` | 거래 심볼 (예: `BTCUSDT`, `ETHUSDT`) |
| `--entry` | 진입가 (지정가 또는 현재가 모두 가능 — 5-Layer Layer 3가 자동 분기) |
| `--sl` | Stop Loss 가격 |
| `--tp` | Take Profit 가격 |
| `--direction` | `long` 또는 `short` (대소문자 무관) |

### 출력 예시 (4/5 통과 케이스)

```
=== Setup Validation ===
Symbol: BTCUSDT
Direction: LONG
Entry: 80,937.00 / SL: 80,637.00 / TP: 81,837.00

📊 5-Layer Evaluation:
✅ Layer 1: 4H 강세 정렬 (MA25 80,633.80 > MA99 78,724.24)
✅ Layer 2: 핵심 레벨 근처 (swing_high 81,080.00, 거리 0.18%)
❌ Layer 3: 거부 캔들 + 거래량 미충족
✅ Layer 4: SL이 swing low (80,725.09) 근처 (0.11%)
✅ Layer 5: R:R 3.00 (≥ 3.0)

🎯 Score: 4/5
🟢 Recommendation: ENTER

ℹ Advisory (진행중 15m, 점수 무영향):
  진행중 15m: 거부 패턴 미형성
```

### Exit codes

| Code | 의미 |
|---|---|
| 0 | 정상 종료 (셋업 통과 여부와 무관) |
| 2 | 입력 검증 실패 (예: LONG에서 SL이 Entry보다 큼) |
| 3 | Binance API 호출 실패 (네트워크/4xx/5xx) |

---

## 출력 해석

### Layer별 아이콘

| 아이콘 | 의미 | 점수 |
|---|---|---|
| ✅ pass | 조건 충족 | 1점 |
| ❌ fail | 조건 미충족 | 0점 |
| ⏸ pending | 평가 불가 (Layer 3 한정 — entry 근처를 50h 내 미방문) | 0점 |

### Score → Recommendation

- **5/5**: 🟢 ENTER (강한 셋업)
- **4/5**: 🟢 ENTER (진입 가능 셋업의 최소 기준)
- **≤ 3/5**: 🚫 PASS (이 셋업은 무시할 것)

### Alert Tier (Scanner 전용)

스캐너는 **CONFIRMED**와 **FORMING** 두 종류 알람을 구분합니다:

- 🟢 **CONFIRMED**: 마감된 캔들 기준 5-Layer 모두 통과 (전통적 4/5+ 셋업)
- ⚡ **FORMING**: 진행중 15m 캔들에서 거부 패턴 + 거래량 spike가 *형성 중*. Layer 1,2,4,5는 confirmed, Layer 3만 아직 마감 안 됨.

FORMING 알람은 별도 idempotency signature를 쓰므로 같은 셋업이 forming → confirmed로 한 번씩 두 번 알람됩니다. **FORMING은 monitor 신호이지 진입 신호가 아닙니다** — 15m 마감 후 confirmed로 올라오는지 확인 후 결정.

### Pending 케이스

`⏸ Layer 3: PENDING` 표시는 entry 가격 근처를 최근 50시간 동안 한 번도 방문하지 않았다는 뜻입니다 (fresh level). 이 경우:
- 점수에는 0점으로 반영됨 (즉 다른 4개가 모두 통과해야 4/5 도달)
- entry까지 가격이 도달하면 도구를 다시 실행하세요. 그때 Layer 3가 실제 거부 캔들을 평가할 수 있습니다.

### Advisory

`ℹ Advisory (진행중 15m)`는 현재 형성중인 15분봉의 상태를 알려주는 정보 라인입니다. **점수에는 영향 없음**. 진행중 캔들의 모양은 마감 직전 뒤집힐 수 있으므로 score 계산에는 마감된 캔들만 사용합니다 (재현성 + 체리피킹 방지).

---

## 동기

BTC 선물 트레이딩에서 진입 결정을 더 일관되게 만들기 위함. 좋은 셋업과 나쁜 셋업을 객관적으로 평가하지 못해서 발생하는 실수(약한 셋업에 진입, R:R 부족, 핵심 레벨이 아닌 곳에서 진입 등)를 방지.

직접 정립한 5-Layer 평가 프레임워크를 자동화한다.

---

## 5-Layer 평가 프레임워크

각 레이어 1점, 총 5점 만점. **4점 이상**이 진입 가능 셋업의 기준.

### Layer 1: 큰 추세 정합성 (4시간봉)

4H MA(25)와 MA(99) 정렬 확인:

- **LONG**: MA25 > MA99 (강세 정렬)이면 통과
- **SHORT**: MA25 < MA99 (약세 정렬)이면 통과
- 그 외: 실패

### Layer 2: Setup Zone (진입가가 핵심 레벨 근처?)

1시간봉에서 swing high/low 자동 식별:

- 최근 100개 1H 캔들 분석
- swing high: 좌우 N개 캔들보다 높은 고점 (**N=5**, BTC 변동성에서 노이즈 필터)
- swing low: 좌우 N개 캔들보다 낮은 저점
- 핵심 MA들도 레벨로 취급: 1H MA(25), MA(99)
- **통과 조건**: 진입가가 어떤 핵심 레벨의 ±0.3% 이내

### Layer 3: 진입 트리거 (Hybrid Historical)

Real-time과 plan 모드를 단일 알고리즘으로 처리:

1. 최근 50개 1H 캔들 중 [low, high] 범위가 entry ±0.3% 와 겹치는 가장 최근 캔들 탐색
2. 발견 시 → 그 1시간 구간의 15m 캔들 4개에서 거부 패턴 + 거래량 평가
3. 미발견 시 → ⏸ pending (점수 0)

**캔들 패턴:**
- 망치형 (Hammer): `lower_wick > 2 × body` AND `upper_wick < body`
- 슈팅스타 (Shooting Star): `upper_wick > 2 × body` AND `lower_wick < body`

**거래량 기준:**
- 해당 캔들 거래량 > 직전 10개 15m 캔들 평균 거래량 × 1.5

**통과 조건:**
- LONG: 망치형 + 거래량 만족 (4개 15m 중 어느 하나)
- SHORT: 슈팅스타 + 거래량 만족 (4개 15m 중 어느 하나)

### Layer 4: SL이 구조 기반? (단방향)

- **LONG**: SL이 swing low의 0~0.5% **아래** 구간에 위치 (`swing_low × 0.995 ≤ SL ≤ swing_low`)
- **SHORT**: SL이 swing high의 0~0.5% **위** 구간에 위치 (`swing_high ≤ SL ≤ swing_high × 1.005`)
- 그 외: 실패

이유: SL은 항상 level *너머*에 있어야 보호 의미. swing low *위*에 SL을 두면 swing low가 안 깨져도 SL hit되어 보호 의미 상실.

### Layer 5: R:R ≥ 3.0

방향별 계산:

- **LONG**: `reward = TP - Entry`, `risk = Entry - SL`
- **SHORT**: `reward = Entry - TP`, `risk = SL - Entry`
- `R:R = reward / risk`
- ≥ 3.0이면 통과

---

## 사용 룰 (도구 사용자가 지킬 것)

도구는 보조이지 의사결정자가 아님. 다음 룰 준수:

1. **한 주 진입 횟수 ≤ 3회**
2. **Score 4/5 미만 = 무조건 패스**
3. **도구 출력에 100% 의존하지 않음** — 최종 판단은 본인의 몫
4. **진입 후 plan stick** — 도구 결과로 SL/TP 변경 금지

---

## 한계

이 도구가 **하지 않는** 것:

- **백테스트 / 통계**: 과거 셋업의 승률을 측정하지 않음. 실제 트레이드 결과 추적은 Phase 2 예정.
- **실시간 모니터링**: 매 실행마다 fetch + 평가하는 일회성 도구. 자동 스캔은 Phase 1 예정.
- **Multi-symbol 동시 평가**: 한 번에 하나의 symbol만. Phase 1에서 multi-symbol 지원.
- **시장 미시구조**: Order book, funding rate, open interest 등 거시 지표 반영 안 함.
- **거짓 양성 0% 보장**: 5-Layer는 통계적 휴리스틱이며 모든 셋업이 수익으로 연결되지 않음. 룰 #1 (주 3회 제한)을 반드시 지키세요.
- **Telegram/이메일 알람**: Phase 1 예정.

---

## 기술 스택

- **언어**: Python 3.10+
- **라이브러리**:
  - `pandas` (지표 계산, MA는 `rolling().mean()`)
  - `requests` (Binance API)
  - `pytest`, `pytest-mock` (테스트)
- **API 키**: 불필요 (Binance public endpoints)

---

## 데이터 소스

Binance Public API:

- **Klines**: `GET https://api.binance.com/api/v3/klines`
  - intervals: `4h`, `1h`, `15m`
  - limit: `100` (4h, 1h), `200` (15m — Hybrid Historical 50시간 커버 위함)
  - 인증 불필요
- **마감된 캔들만 사용**: 진행중 캔들은 자동으로 drop (재현성 보장)

캐싱 안 함 — 매 실행마다 최신 데이터 fetch.

---

## 프로젝트 구조

```
trade-validator/
├── validate.py            # 메인 CLI 엔트리
├── data/
│   └── binance.py         # Binance API 호출 + 진행중 캔들 drop
├── analysis/
│   ├── indicators.py      # MA 계산
│   ├── levels.py          # swing high/low 식별 (N=5)
│   ├── candles.py         # 망치형/슈팅스타 + volume spike
│   └── layers.py          # 5개 레이어 + 입력 검증
├── output/
│   └── formatter.py       # 결과 포맷팅 + advisory
├── tests/
│   ├── fixtures/          # Binance API 응답 JSON (오프라인 테스트용)
│   └── test_*.py          # pytest 파일
├── requirements.txt
└── README.md
```

---

## 테스트

```bash
# 기본 (오프라인 — 151 tests)
.venv/bin/python -m pytest tests/ -v

# Live (실제 Binance API 호출 — 6 tests, 약 3초)
.venv/bin/python -m pytest tests/ -m live --run-live -v
```

총 157 tests:
- **151 offline** — 순수 함수, fixture 기반, Mock 기반, CLI, edge case, stress
- **6 live** (default skipped) — 실제 Binance schema 회기, cron 환경 시뮬레이션

CI는 offline만 실행. Live는 수동 실행 또는 production 검증용.

---

## 향후 확장 (이번 Phase 0에서는 만들지 말 것)

- **Phase 1**: 5분마다 자동 스캔 + Telegram bot 알람
- **Phase 2**: SQLite 저널 (진입한 트레이드 결과 추적, 본인 통계)
- **Phase 3**: Multi-symbol 지원, LLM 통합 (Ollama 또는 Claude API)

---

## 자동 스캔 (cron 등록)

`scan.py`는 one-shot 스캐너입니다. 5분마다 실행은 cron이 담당합니다.

### 1. config.toml 준비

```bash
cp config.example.toml config.toml
# 필요한 심볼 / 알림 채널 편집
```

### 2. crontab 등록

```bash
crontab -e
```

다음 라인 추가 (5분마다 실행):

```
*/5 * * * * cd /Users/bokwon/trade-validator && .venv/bin/python scan.py --quiet --config config.toml --state .tv-state.json >> scan.log 2>&1
```

플래그 의미:
- `--quiet` — 셋업이 dispatch될 때만 출력 (cron이 메일 안 보내게)
- `--config` — 명시적 config 경로 (cron의 cwd가 다를 수 있음)
- `--state` — alert idempotency state file 위치
- 출력 리다이렉트 — cron 실행 로그를 `scan.log`로 누적

### 3. Exit codes

| Code | 의미 |
|---|---|
| 0 | 정상 완료 (셋업 통과 여부와 무관) |
| 2 | Config 에러 |
| 3 | 모든 심볼 실패 (Binance 장애 등 — 운영자 알림 필요) |

### 4. 로그 확인

```bash
tail -f /Users/bokwon/trade-validator/scan.log
```

스캐너는 24시간 윈도우로 같은 (symbol, direction, swing low) 셋업을 한 번만 알림합니다. swing low가 ±0.1% 이상 다르면 새 셋업으로 인식.

---

## Trade Journal (Phase 4)

실제 트레이드 결과를 SQLite에 기록해서 framework 정확도 검증.

### 사용 흐름

```bash
# 1) alerts.jsonl을 DB에 import (한 번만 또는 정기적으로)
python journal.py migrate

# 2) 알람 받음 (Telegram에 "Alert ID: BTC-202605110430-confirmed")

# 3a) 진입했으면 trade 기록
python journal.py take BTC-202605110430-confirmed \
    --entry 80937 --sl 80637 --tp 81837 --size 1000

# 3b) 건너뛰었으면 skip 기록 (왜 건너뛰었는지 메모)
python journal.py skip BTC-202605110430-confirmed --reason "macro bearish"

# 4) 열린 trade 확인
python journal.py open

# 5) trade 종료 후 결과 기록
python journal.py close 1 --price 81600 --reason "tp_near"

# 6) 누적 통계 (framework 정확도)
python journal.py stats --days 30
```

### Stats 출력 예시

```
=== Trade Stats (최근 30일) ===
  Trades: 12 (8W / 4L)
  Win rate: 66.7%
  Avg PnL: +1.45%
  Avg win: +3.20%  |  Avg loss: -2.05%
  Expectancy: +1.45% per trade

  Tier 1 score별:
    4/5: 9 trades, win_rate=56%, avg=+0.80%
    5/5: 3 trades, win_rate=100%, avg=+3.40%

  Agent verdict별:
    ENTER: 5 trades, win_rate=80%, avg=+2.10%
    WATCH: 7 trades, win_rate=57%, avg=+1.00%
```

→ **5/5가 4/5보다 훨씬 정확한지**, **Agent ENTER가 실제로 가치 있는지** 데이터로 답.

---

## Agentic 분석 설정 (Phase 2 옵션)

Tier 1 (5-Layer) 위에 LLM specialist agents를 얹어 알람 컨텍스트를 풍부하게 만듭니다.
**Tier 1 verdict는 절대 upgrade 안 됨** (downgrade-only 룰 — framework discipline 보존).

### Backend 선택

```bash
# .tv-env에 설정 (없으면 default 자동 선택)
export AGENT_BACKEND="cli"   # plan quota (Claude Pro/Max), 권장
# 또는
export AGENT_BACKEND="api"   # API credits — ANTHROPIC_API_KEY 필요
# 또는
export AGENT_BACKEND="none"  # agentic tier 비활성
```

`AGENT_BACKEND` 미설정 시: `claude` CLI 있으면 CLI 사용, 아니면 API 시도, 둘 다 없으면 자동 비활성.

### Model 선택 (옵션)

```bash
# 기본은 Sonnet 4.6 — Pro plan에서 사실상 무제한
export AGENT_MODEL="sonnet"     # CLI alias (default)
export AGENT_MODEL="opus"       # 더 깊은 분석 (Pro plan 제한 있음)
export AGENT_MODEL="haiku"      # 빠르고 quota 절약
# API 백엔드는 full name 필요: claude-sonnet-4-6, claude-opus-4-7 등
```

### 출력 예시

```
🎯 Score: 4/5
🟡 Recommendation: WATCH

🤖 Agent Verdict: WATCH  (confidence 58%)
   → specialists 분석 결과 Tier 1 유지
```

Downgrade 시:
```
🤖 Agent Verdict: WATCH (Tier 1: ENTER → WATCH)  (confidence 65%)
   → microstructure: weak rejection 감지
```

---

## Telegram 알람 설정 (Phase 1 옵션)

스캐너 알람을 텔레그램으로 받으려면:

### 1. 봇 만들기

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 검색 → 대화 시작
2. `/newbot` 입력 → 안내에 따라 봇 이름과 username 설정
3. 출력된 **Bot Token** 복사 (예: `1234567890:AAH...`)

### 2. Chat ID 확인

1. 만든 봇 검색해서 `/start` 메시지 보내기 (봇이 사용자에게 메시지를 보내려면 사용자가 먼저 시작해야 함)
2. 브라우저에서 `https://api.telegram.org/bot<TOKEN>/getUpdates` 열기
3. 응답에서 `"chat":{"id":<NUMBER>}` 부분의 숫자가 chat_id

### 3. 환경 변수 + config.toml

```bash
export TELEGRAM_BOT_TOKEN="1234567890:AAH..."
export TELEGRAM_CHAT_ID="123456789"
```

`config.toml`:
```toml
[notifications]
channels = ["stdout", "telegram"]

[notifications.telegram]
enabled = true
```

토큰이나 chat_id가 없으면 텔레그램 채널은 자동으로 비활성화됩니다 (다른 채널은 정상 동작).

---

## License

MIT — see [LICENSE](LICENSE).
