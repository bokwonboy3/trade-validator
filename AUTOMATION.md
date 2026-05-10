# Overnight Automation Log

이 문서는 사용자가 잠든 동안 Claude가 `/loop` 자율 모드로 작업하는 동안 상태를 추적합니다.
모든 결정/진행/블로커가 여기에 기록됩니다. 아침에 이 파일을 먼저 보세요.

---

## Scope

**Phase A (S) — 안전 베이스라인 강화**: README, CI, 코드 품질
**Phase B (M) — Phase 1 구현**: Multi-symbol 스캔 + 알람 와이어 (Telegram 토큰 미포함)

순서: A 완료 후 B 진행. A에서 막히면 B 시작 안 함.

---

## Authority

10년차 financial trader 관점 + 엔지니어 판단으로 설계 결정. Phase 0 plan의 D1~D10 결정은 불변(invariant) 처리.

---

## Stop Conditions (강제 종료)

다음 중 하나라도 발생하면 즉시 STOP하고 이 파일에 사유 기록:

1. **외부 블로커**: 자격 증명/사용자 결정이 필수인 시점 (예: Telegram bot 토큰)
2. **Scope 완료**: A + B 모든 항목 완료
3. **Commit 상한**: 누적 30개
4. **시간 상한**: 자동화 시작 후 8시간
5. **반복 실패**: 같은 곳에서 테스트 fail 3회 연속 (방향이 잘못됐다는 신호)

---

## Iteration Loop Spec

매 iteration:
1. `git status` + `git log -3 --oneline` 으로 현재 상태 확인
2. 이 파일의 Progress Log를 읽어 직전 진행 상황 파악
3. 다음 작업 단위 1개 선택 (가장 작은 의미 있는 단위)
4. 구현 + `pytest tests/` + 필요 시 smoke 검증
5. 모두 통과 시: `git add` + `git commit` + `git push`
6. 실패 시: 재시도 1회. 그래도 실패면 다른 항목으로 우회 + 이 파일에 기록
7. Progress Log에 한 줄 추가
8. Stop 조건 점검 → 만족 시 STOP, 아니면 ScheduleWakeup으로 다음 iteration 예약

---

## Phase A (S) — Tasks

| ID | 작업 | DoD | 상태 |
|---|---|---|---|
| A1 | README 보강 — 설치/사용법/출력 해석/한계 | `## Installation`, `## Usage`, `## Interpreting Output`, `## Limitations` 섹션 존재 | ✅ done |
| A2 | `--no-emoji` 플래그 | 출력에서 이모지 제거 옵션 동작 + 테스트 1개 | ✅ done |
| A3 | 타입 힌트 일관화 | `from __future__ import annotations` 모든 모듈, 함수 시그니처 타입 힌트 | ✅ done |
| A4 | Edge case 테스트 추가 | 빈 DataFrame, NaN MA, 캔들 부족 등 5+ 케이스 | ✅ done |
| A5 | GitHub Actions CI | `.github/workflows/test.yml` — push/PR에 pytest 실행, fixture만 사용 | ✅ done |
| A6 | LICENSE (MIT) | `LICENSE` 파일 존재 | ✅ done |
| A7 | 에러 메시지 정리 | `BinanceError` / `InputError` 메시지 톤 통일 | ✅ done |

---

## Phase B (M) — Tasks

| ID | 작업 | DoD | 상태 |
|---|---|---|---|
| B1 | Config 스키마 (TOML) | `config.example.toml` — symbols, intervals, defaults | ✅ done |
| B2 | Refactor: `evaluate_setup()` 추출 | `validate.py`의 5-layer 호출을 단일 함수로 | ✅ done |
| B3 | Multi-symbol scanner | `scan.py` — config 읽고 모든 symbol 평가, ≥4/5만 출력 | ✅ done |
| B4 | "이미 알림 보낸 셋업" idempotency | `~/.tv-state.json` 또는 repo 내 state file로 중복 방지 | ✅ done |
| B5 | Notification dispatcher 추상화 | `output/notify.py` — File / Stdout / Telegram(stub) 채널 | ✅ done |
| B6 | Telegram client (토큰 없으면 stub) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` env. 없으면 dry-run 로그 | ✅ done |
| B7 | Cron-friendly entry | `scan.py --once` 단일 스캔, exit code로 성공/실패 표현 | ✅ done |
| B8 | Fixture 기반 scan smoke 테스트 | `pytest tests/test_scan.py` 통과 | pending |

---

## Decision Principles (불변)

- **Trader 보수주의**: 정보 부재 = 약점 처리
- **재현성 > liveness**: 점수는 마감된 캔들만
- **단일 알고리즘 > 플래그 분기**: Hybrid Layer 3 정신 유지
- **Phase 0 invariants**: D1~D10 (plan 파일) 깨지 않음
- **Trader 룰 정신**: "Score 4/5 미만 = 무조건 패스" 무력화될 변경은 거부

---

## 외부 블로커 처리

발견 시:
1. Progress Log에 "BLOCKER: ..." 기록
2. 코드는 stub/placeholder로 작성하고 README/AUTOMATION.md에 사용자 액션 명시
3. 블로커 항목은 SKIP하고 다음 항목으로 진행
4. 모든 항목 시도 후에 STOP

알려진 블로커:
- B6 Telegram 토큰 — 사용자가 BotFather에서 받아 `TELEGRAM_BOT_TOKEN` 환경변수 설정 필요
- B6 Chat ID — `TELEGRAM_CHAT_ID` 환경변수 필요

---

## Progress Log

(매 iteration마다 한 줄씩 추가)

- `[2026-05-10 23:50 KST]` AUTOMATION.md 작성 — 자동화 시작
- `[2026-05-10 23:55 KST]` A1 완료: README 전면 보강 (Installation, Usage, 출력 해석, 한계, exit codes, 테스트 안내). 5-Layer 섹션은 실제 구현(N=5, Hybrid Layer 3, Layer 4 단방향)에 맞춰 업데이트. 작업 요청 섹션 제거. 54 tests still pass.
- `[2026-05-11 00:03 KST]` A2 완료: --no-emoji 플래그 추가. _glyph 헬퍼로 emoji/plain glyph 매핑, 모든 literal emoji 제거 (formatter + validate.py 에러 라인). 테스트 4개 추가, 58 tests pass. Smoke 검증: `--no-emoji` 출력에 이모지 0개, [PASS]/[FAIL]/[ENTER] 라벨 정상.
- `[2026-05-11 00:08 KST]` A3 완료: `Candle = Mapping[str, float]` 타입 별칭 도입 (analysis/candles.py). formatter._build_advisory, ValidationReport.advisory_15m, validate._fetch_advisory_15m 에서 `dict` → `Candle`로 정밀화. layers.py 모듈 상수에 `Final` 적용. 58 tests still pass.
- `[2026-05-11 00:14 KST]` A4 완료: tests/test_edge_cases.py 13개 추가 (빈 df, NaN MA, monotonic 시리즈, 너무 적은 행, Layer 1~4 graceful fail, zero-range candle 등). **Bug fix**: layer_2가 levels 리스트 빈 경우 inf% 출력 → "no_levels_found" reason + formatter에 별도 메시지 ("핵심 레벨 미발견 (1H 데이터 부족)"). 71 tests pass (was 58).
- `[2026-05-11 00:18 KST]` A5 완료: .github/workflows/test.yml 추가. push 모든 브랜치 + PR(main)에서 Python 3.11/3.12 매트릭스로 pytest. pip 캐시 활성화. fixture만 사용해서 인터넷 없이 동작. **CI 결과**: 31초 success (3.11 + 3.12 둘 다 통과).
- `[2026-05-11 00:22 KST]` A6 완료: MIT LICENSE 추가, README에 License 섹션 링크.
- `[2026-05-11 00:25 KST]` A7 완료: 에러 메시지 톤 통일.
  - data/binance.py: _parse_binance_error 헬퍼 추가 (Binance JSON 응답에서 msg 필드 추출). 4xx/5xx 모두 동일한 포맷("HTTP {code}: {msg}").
  - Timeout과 일반 RequestException 분리 (기존엔 한 묶음).
  - 응답 텍스트 길이 일관화 (4xx 500 → 300, 5xx 200 → 300).
  - layers.py: "unknown direction" → "direction must be 'long' or 'short' (got: ...)".
  - Smoke: invalid symbol → "❌ Binance error: HTTP 400: Invalid symbol." (raw JSON 노출 사라짐).
  - 71 tests still pass.

## Phase A 완료
A1~A7 모두 done. Score: 71 tests, CI green, 9 commits on overnight branch.

- `[2026-05-11 00:32 KST]` B2 완료: evaluate_setup() + SetupEvaluation 데이터클래스 (analysis/layers.py 끝에 추가). validate.py main() 5-layer 호출을 단일 함수로 압축. PASS_THRESHOLD=4 상수화. 4 단위 테스트 추가. 83 tests pass. CLI smoke 동일 출력 확인.
- `[2026-05-11 00:35 KST]` B5 완료: output/notify.py — Channel Protocol + Stdout/File/Telegram 구현. build_channels() 팩토리 (telegram disabled 또는 env 미설정 시 stderr 경고 + 스킵), dispatch() per-channel 실패 격리. 16개 테스트 추가, 99 tests pass. Telegram send는 B5 스텁 (B6에서 HTTP 구현).
- `[2026-05-11 00:39 KST]` B6 완료: TelegramChannel.send 실제 HTTP POST 구현 (api.telegram.org/bot{token}/sendMessage). 4096자 초과 시 자동 truncate, 4xx/5xx 또는 ok=false 시 RuntimeError → dispatcher가 격리. requests.post mock 으로 5개 테스트 추가 (성공, 401, ok=false, truncate, 격리). README에 텔레그램 봇 설정 가이드 추가 (BotFather, getUpdates, env+config). 103 tests pass.
- `[2026-05-11 00:43 KST]` B3 완료: scan.py + analysis/scanner_logic.py.
  - determine_direction(): Layer 1 mirror (MA25 vs MA99, ties → None)
  - synthesize_setup(): entry=last 1H close, SL=closest qualifying swing × (1±buffer 0.3%), TP=R:R 3.0 by construction. 2% 거리 제한.
  - sl_buffer_pct >= LAYER4_TOLERANCE 인 경우 ValueError (안전장치)
  - scan.py: ScanResult 데이터클래스, scan_symbol/scan/format_summary_line/run_scan/main
  - 11개 단위 테스트 (scanner_logic), 114 tests pass.
  - **라이브 스모크 성공**: BTCUSDT/ETHUSDT/SOLUSDT 모두 4/5 통과 (Layer 3 fail = 거부 캔들 부재, 나머지 4개 pass — 예상 동작).
- `[2026-05-11 00:51 KST]` B4 완료: alert_state.py — JSON 기반 idempotency.
  - AlertState.load/save (atomic write via .tmp+rename)
  - already_alerted: (symbol, direction, sl_swing_price ±0.1%) 매칭, TTL 24h
  - 로드 시 stale 자동 prune
  - scan.run_scan 통합: passing → already_alerted 검사 → 새것만 dispatch + record
  - "X suppressed (already alerted within 24h)" 출력
  - 11개 단위 테스트, 125 tests pass.
- `[2026-05-11 00:56 KST]` B7 완료: scan.py CLI 인자 + exit codes + cron docs.
  - `--config` (경로 오버라이드), `--state` (state 경로), `--once` (호환용 플래그), `--quiet` (cron 모드 — 메타 출력 억제, 에러는 stderr 유지)
  - Exit codes: 0=ok, 2=config error, 3=all symbols failed
  - run_scan: all-failed 검지 → exit 3. partial errors는 stderr만 노출하고 exit 0.
  - README: "자동 스캔 (cron 등록)" 섹션 추가 (crontab 예시 + exit code 표 + 로그 안내)
  - .gitignore: scan.log 추가
  - 9개 단위 테스트 (parse_args, exit codes, quiet 동작, idempotency 통합), 134 tests pass.
  - **라이브 검증**: 1차 실행 → 3 setup dispatch + state 저장. 2차 실행 → 0 출력, exit 0 (모두 suppressed) — idempotency 정상.
- `[2026-05-11 00:28 KST]` B1 완료: scanner_config.py + config.example.toml.
  - tomllib (Python 3.11+ stdlib) 사용 — 추가 의존성 0
  - frozen dataclass: ScannerConfig / ThresholdsConfig / NotificationsConfig / FileChannelConfig / TelegramChannelConfig
  - ConfigError 친근한 메시지 ("config not found — copy config.example.toml → config.toml")
  - 검증: symbols 비어있음, min_score 범위, default_rr 양수, channels 화이트리스트
  - .gitignore에 config.toml, alerts.log, .tv-state.json 추가
  - 8개 단위 테스트, 79 tests pass.
