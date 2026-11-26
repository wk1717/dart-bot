# 종목 하나 조회가 실패하면 정산 전체가 멈춤

- 발생: 2026-11-23 (PR #11 CodeRabbit 리뷰로 발견, 운영에서 실제로 터진 적은 없음)
- 관련: `trader.py` `settle()`, `fill_buy_prices()`, GitHub Actions 15:40 정산
- 상태: 수정 완료 (fe2f3d5, e259228)

## 증상

실제로 겪진 않음. CodeRabbit이 지적한 시나리오:

> If an unrelated open trade has a null buy_price and its lookup fails, fill_buy_prices() raises before this function selects any sold trades.

## 원인

정산 흐름이 하나의 예외에 다 묶여 있었음.

1. `settle()` 맨 앞에서 `fill_buy_prices()` (보유 중 매수가 채우기) 호출. 화면용 부가 작업
2. 그 안에서 한 종목의 KIS 조회가 실패하면 예외가 그대로 올라감
3. 매도 정산, 수익 알림, 잔고 저장이 전부 안 돌아감. 다음 날도 같은 종목에서 또 멈출 수 있음

정산 루프 안에서도 똑같음. 매도 종목 하나의 체결 조회가 실패하면 나머지 종목도 정산 안 됨.

## 해결

부가 작업과 본 작업, 종목과 종목을 서로 떼어냄.

- `fill_buy_prices()`: 종목마다 try/except. 실패하면 에러 알림만 보내고 다음 종목으로
- `settle()` 정산 루프: 종목마다 try/except. 실패한 종목은 알림에 "실패 ○○: 사유" 줄로 남기고 나머지는 정산. 실패한 건 정산 안 된 상태로 남아서 다음 날 다시 시도
- 잔고 저장(`save_cash()`)은 정산 뒤로 옮김. 잔고 저장이 실패해도 정산은 이미 끝난 상태

## 수치

측정 안 함 (운영에서 실패가 난 적 없음). 동작은 아래 테스트로 확인.

## 재발 방지

- `test_fill_buy_prices`: 첫 종목 조회가 `KIS 500`으로 실패해도 다음 종목 매수가가 채워지고 에러 알림이 가는지
- `test_settle_skips_failed_lookup`: 두 종목 중 하나가 실패해도 나머지는 DB에 정산되고, 알림에 "실패 나: KIS 500"이 들어가고, 잔고 저장이 불리는지
