# KIS 모의투자 체결 조회가 주문별 목록을 안 줌

- 발생: 2026-11-23
- 관련: KIS Open API 모의투자 `inquire-daily-ccld` (VTTC8001R), `trader.py`
- 상태: 수정 완료 (8c646f8), 정산 1건 실제 성공 확인 (드림씨아이에스 11/24)

## 증상

모의투자 탭에서 보유 중인 삼익악기(11/23 매수), 드림씨아이에스(11/20 매수)의 매수가가 비어 있음. DB `trades.buy_price`가 둘 다 null.

## 원인

`fills()`가 체결 조회 응답의 주문별 목록 `output1`만 읽었음.

```python
return {r["odno"].lstrip("0"): (int(r["tot_ccld_qty"]), float(r["avg_prvs"]))
        for r in res["output1"] if int(r["tot_ccld_qty"]) > 0}
```

실제 응답을 찍어보니 모의투자는 `output1`이 늘 빈 리스트고 합계 `output2`만 채워져 옴.

```
PDNO=002450(삼익악기), 20261123:     output1 [], output2 {'tot_ccld_qty': '1000', 'tot_ccld_amt': '991000', 'pchs_avg_pric': '991.0000'}
PDNO=223250(드림씨아이에스), 1120~1123: output1 [], output2 {'tot_ccld_qty': '312', 'tot_ccld_amt': '1001520', ...}
종목 안 좁힘:                          output1 [], output2 {'tot_ccld_qty': '1312', 'tot_ccld_amt': '1992520', ...}
```

- 그냥 화면 버그가 아니었음. 같은 함수를 쓰는 15:40 정산도 매번 "체결 내역 없음"으로 끝날 상태였음
- `ODNO`(주문번호)로 좁히는 파라미터는 모의투자에서 무시됨. `PDNO`(종목)와 `SLL_BUY_DVSN_CD`(01 매도, 02 매수)는 먹음

## 해결

종목·매수매도·날짜로 좁힌 합계에서 평균가를 계산함.

```python
def fill(code, side, day):
    res = kis(..., {"PDNO": code, "SLL_BUY_DVSN_CD": {"sell": "01", "buy": "02"}[side], ...})
    qty = int(res["output2"]["tot_ccld_qty"] or 0)
    return (qty, int(res["output2"]["tot_ccld_amt"]) / qty) if qty else None
```

- 같은 날 같은 종목을 두 번 사는 일은 없음 (보유 중이면 안 삼). 그래서 합계를 써도 주문 하나와 같음
- 보유 중인 거래의 매수가를 매수 직후와 정산 앞에서 채우는 `fill_buy_prices()`를 추가함

## 수치

| | before | after |
|---|---|---|
| 보유 종목 매수가 | 2종목 모두 null | 삼익악기 991원, 드림씨아이에스 3,210원 |
| KIS 잔고 평단과 비교 | - | 둘 다 일치 |
| 정산 | 측정 안 함 (매도 전이었음) | 드림씨아이에스 11/24 정산 성공, 312주 3,210 → 3,690원 |

측정: 위 응답은 `trader.kis()`로 VTTC8001R을 직접 호출해서 찍음. 평단은 잔고 조회 VTTC8434R의 `pchs_avg_pric`.

## 재발 방지

- `test_fill_uses_summary`: `output1`이 비고 `output2`만 있는 응답에서 (312, 3210.0)이 나오는지, 체결 없으면 None인지 확인
- 정산에서 체결을 못 찾으면 텔레그램에 "확인 안 됨 ○○: 체결 내역 없음"으로 뜸
