# Trade Validator (Phase 0 MVP)

BTC perpetual futures 셋업을 5-Layer 프레임워크로 자동 평가하는 CLI 도구.

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

- 최근 50개 1H 캔들 분석
- swing high: 좌우 N개 캔들보다 높은 고점 (N=3 default)
- swing low: 좌우 N개 캔들보다 낮은 저점
- 핵심 MA들도 레벨로 취급: 1H MA(25), MA(99)
- **통과 조건**: 진입가가 어떤 핵심 레벨의 ±0.3% 이내

### Layer 3: 진입 트리거 (15분봉 거부 캔들)

최근 3개 15m 캔들 분석:

**캔들 패턴 정의:**
- 망치형 (Hammer): `lower_wick > 2 × body` AND `upper_wick < body`
- 슈팅스타 (Shooting Star): `upper_wick > 2 × body` AND `lower_wick < body`

**거래량 기준:**
- 해당 캔들 거래량 > 직전 10개 캔들 평균 거래량 × 1.5

**통과 조건:**
- LONG: 망치형 + 거래량 만족
- SHORT: 슈팅스타 + 거래량 만족

### Layer 4: SL이 구조 기반?

- LONG: SL이 최근 50개 1H 캔들의 swing low 아래 (±0.5% 이내)
- SHORT: SL이 최근 50개 1H 캔들의 swing high 위 (±0.5% 이내)
- 그 외: 실패

### Layer 5: R:R ≥ 3.0

방향별 계산:

- **LONG**: `reward = TP - Entry`, `risk = Entry - SL`
- **SHORT**: `reward = Entry - TP`, `risk = SL - Entry`
- `R:R = reward / risk`
- ≥ 3.0이면 통과

---

## 입력 / 출력

### CLI 사용법

```bash
python validate.py --symbol BTCUSDT --entry 80841 --sl 80500 --tp 81700 --direction long
```

### 출력 예시

```
=== Setup Validation ===
Symbol: BTCUSDT
Direction: LONG
Entry: 80,841 / SL: 80,500 / TP: 81,700

📊 5-Layer Evaluation:
✅ Layer 1: 4H 강세 정렬 (MA25 80,123 > MA99 79,456)
❌ Layer 2: 핵심 레벨 아님 (가장 가까운 레벨: 80,377, 거리 0.57%)
❌ Layer 3: 거부 캔들 없음
✅ Layer 4: SL이 swing low (80,420) 근처
❌ Layer 5: R:R 1.85 (3.0 미달)

🎯 Score: 2/5
🚫 Recommendation: PASS

💡 Suggestions:
- Layer 2: 80,377 또는 81,700 도달 후 평가
- Layer 5: TP 동일 시 SL을 80,624로 이동하면 R:R 3.0
```

---

## 기술 스택

- **언어**: Python 3.10+
- **라이브러리**:
  - `pandas`, `pandas-ta` (지표 계산)
  - `requests` (Binance API)
  - `argparse` (CLI 파싱)
- **API 키**: 불필요 (Binance public endpoints)

---

## 데이터 소스

Binance Public API:

- **Klines**: `GET https://api.binance.com/api/v3/klines`
  - intervals: `4h`, `1h`, `15m`
  - limit: `100` (4h, 1h), `50` (15m)
  - 인증 불필요

캐싱 안 함 — 매 실행마다 최신 데이터 fetch.

---

## 프로젝트 구조 (제안)

```
trade-validator/
├── validate.py            # 메인 CLI 엔트리
├── data/
│   └── binance.py         # Binance API 호출
├── analysis/
│   ├── indicators.py      # MA 계산
│   ├── levels.py          # 핵심 레벨 식별 (swing high/low)
│   ├── candles.py         # 캔들 패턴 인식
│   └── layers.py          # 5개 레이어 각각 평가
├── output/
│   └── formatter.py       # 결과 포맷팅
├── requirements.txt
└── README.md
```

---

## 요구 사항

1. **빠른 실행**: 전체 평가 < 5초
2. **깨끗한 출력**: 이모지, 색상(옵션) 활용한 가독성 높은 포맷
3. **구체적 피드백**: 각 레이어 실패 시 이유 + 개선 제안
4. **항상 최신 데이터**: 캐싱 없이 매번 Binance에서 fetch
5. **에러 핸들링**:
   - Binance API 오류 (타임아웃, 5xx 등) 처리
   - 잘못된 입력 (예: SL이 LONG에서 entry보다 높을 때) 검증
   - 명확한 에러 메시지

---

## 향후 확장 (이번 Phase 0에서는 만들지 말 것)

- **Phase 1**: 5분마다 자동 스캔 + Telegram bot 알람
- **Phase 2**: SQLite 저널 (진입한 트레이드 결과 추적, 본인 통계)
- **Phase 3**: Multi-symbol 지원, LLM 통합 (Ollama 또는 Claude API)

---

## 사용 룰 (도구 사용자가 지킬 것)

도구는 보조이지 의사결정자가 아님. 다음 룰 준수:

1. **한 주 진입 횟수 ≤ 3회**
2. **Score 4/5 미만 = 무조건 패스**
3. **도구 출력에 100% 의존하지 않음** — 최종 판단은 본인의 몫
4. **진입 후 plan stick** — 도구 결과로 SL/TP 변경 금지

---

## 작업 요청 (Claude Code 향)

지금 즉시 구현 시작하지 말고, 먼저 다음을 해줘:

1. **요구사항 검토 후 명확하지 않은 부분 질문**
2. **5-Layer 평가 로직 중 모호한 부분 확인**
   - Layer 2: swing high/low 식별 알고리즘 (window size, 노이즈 처리)
   - Layer 3: 거래량 기준 1.5배가 적절한지, 다른 방법은?
   - Layer 4: SL 위치 판정의 ±0.5% 허용 범위 적절한지
3. **프로젝트 구조 제안 검토** (위 구조 OK인지, 더 나은 방법 있는지)
4. **단계별 구현 계획** (작은 단위로 쪼개서 — Definition of Done 명확히)
5. **테스트 전략** (단위 테스트 어떻게 할지, mock 데이터 사용할지)
6. **잠재적 위험 / 함정** (Binance API rate limit, 시간대 이슈, 휴장일 등)

내가 plan 검토하고 OK 하면 그때 구현 시작.

위 6가지 정리해서 보여줘.
