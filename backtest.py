"""모의투자 봇 규칙 그대로 과거에 돌려보기

python3 backtest.py   (buyback.py를 먼저 돌려서 data/ 캐시가 있어야 함)

봇과 같은 규칙: 공시 다음 거래일 시가 매수(수량은 전날 종가 기준) · 종목당 100만원 · 최대 10종목 ·
매입 비율 1% 이상 큰 순 · 매수일 포함 3거래일째 종가 매도
비용: 매매 수수료 양쪽 + 매도 증권거래세
결과는 data/backtest.csv (날짜별 평가금액, 코스피·코스닥 같은 돈 넣었을 때)
"""
import csv
import json
from collections import defaultdict

import analyze
import bot
import buyback
import trader

START_CASH = 10_000_000
FEE = 0.00015   # 매매 수수료 (KIS 온라인 기준, 계좌마다 다름)
TAX = 0.0020    # 매도 증권거래세 2026년 (코스피는 농특세 포함 0.20%, 코스닥 0.20%)
OUT_PATH = bot.ROOT / "data" / "backtest.csv"


def simulate(signals, days, prices, fee=FEE, tax=TAX):
    """signals: {매수일: [(매입 비율, 종목코드, 이름)]}, days: 거래일 목록, prices: {코드: {날짜: (시가, 종가, 전날 종가)}}
    → (날짜별 평가금액, 거래 목록)"""
    cash, held, trades, equity, last = START_CASH, [], [], [], {}
    for n, d in enumerate(days):
        # 아침: 봇은 08:40에 사고 15:20에 팖 → 오늘 팔 종목도 아직 보유 수에 들어감
        codes = {h["code"] for h in held}
        for ratio, code, name in sorted(signals.get(d, []), reverse=True):
            if len(held) >= trader.MAX_HOLD:
                break
            px = prices.get(code, {}).get(d)
            if ratio < trader.MIN_RATIO or code in codes or not px:  # 그날 거래 안 됐으면 못 삼
                continue
            qty = trader.BUDGET // px[2]
            if qty == 0:
                continue
            cash -= qty * px[0] * (1 + fee)
            codes.add(code)
            held.append({"code": code, "name": name, "qty": qty, "buy": px[0], "buy_date": d, "n": n})
        # 장 마감: 3거래일째 종가로 팖. 거래정지면 다시 거래되는 날 종가로
        for h in held[:]:
            px = prices[h["code"]].get(d)
            if px:
                last[h["code"]] = px[1]
            if n - h["n"] >= trader.HOLD_DAYS - 1 and px:
                cash += h["qty"] * px[1] * (1 - fee - tax)
                held.remove(h)
                net = px[1] * (1 - fee - tax) / (h["buy"] * (1 + fee)) - 1
                trades.append({**h, "sell": px[1], "sell_date": d, "ret": net})
        equity.append((d, cash + sum(h["qty"] * last[h["code"]] for h in held)))
    return equity, trades


def mdd(values):
    peak, worst = values[0], 0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def load():
    """buyback.py가 만든 캐시로 신호와 가격 준비"""
    events = [e for e in analyze.load_events() if e["category"] == "자사주취득" and e["corp_cls"] in bot.LISTED]
    detail = json.loads(buyback.DETAIL_PATH.read_text())
    shares = json.loads(buyback.SHARES_PATH.read_text())
    corp, offset = {}, 0
    while True:
        page = bot.sb("GET", f"disclosures?select=rcept_no,corp_code,corp_name&category=eq.자사주취득"
                             f"&order=rcept_no&limit=1000&offset={offset}")
        if not page:
            break
        corp.update((r["rcept_no"], r) for r in page)
        offset += 1000
    days = [d for d, _, _ in analyze.load_prices("KOSPI")]
    prices, signals = {}, defaultdict(list)
    for e in sorted(events, key=lambda e: e["rcept_no"]):
        if "정정" in e["report_nm"]:
            continue
        qty = trader.buyback_qty(detail.get(corp[e["rcept_no"]]["corp_code"]) or {}, e["rcept_no"])
        total = shares.get(e["stock_code"])
        buy_day = next((d for d in days if d > e["rcept_dt"]), None)
        if not qty or not total or not buy_day:
            continue
        if e["stock_code"] not in prices:
            rows = analyze.load_prices(e["stock_code"])
            prices[e["stock_code"]] = {d: (o, c, rows[i - 1][2]) for i, (d, o, c) in enumerate(rows) if i}
        signals[buy_day].append((qty / total, e["stock_code"], corp[e["rcept_no"]]["corp_name"]))
    # ponytail: 상장주식수가 지금 기준이라 오래된 공시는 매입 비율이 어긋남. 정확히 하려면 DART 주식총수현황
    start = min(signals)
    return signals, [d for d in days if d >= start], prices


def report(equity, trades, days, label):
    values = [v for _, v in equity]
    rets = [t["ret"] for t in trades]
    print(f"[{label}] {days[0]}~{days[-1]}  거래 {len(trades)}건  "
          f"수익률 {values[-1] / START_CASH - 1:+.1%}  최대낙폭 {mdd(values):.1%}  "
          f"거래당 평균 {sum(rets) / len(rets):+.2%}  승률 {sum(r > 0 for r in rets) / len(rets):.0%}")


def main():
    bot.check_env()
    signals, days, prices = load()
    equity, trades = simulate(signals, days, prices)
    gross, gross_trades = simulate(signals, days, prices, fee=0, tax=0)
    report(gross, gross_trades, days, "비용 전")
    report(equity, trades, days, "비용 후")
    index = {s: dict((d, c) for d, _, c in analyze.load_prices(s)) for s in ["KOSPI", "KOSDAQ"]}
    for s, px in index.items():
        print(f"[{s}] 같은 기간 {px[days[-1]] / px[days[0]] - 1:+.1%}")

    by_year = defaultdict(list)
    for t in trades:
        by_year[t["sell_date"][:4]].append(t["ret"])
    for y, r in sorted(by_year.items()):
        print(f"  {y}: {len(r)}건, 거래당 평균 {sum(r) / len(r):+.2%}")

    with OUT_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "bot", "KOSPI", "KOSDAQ"])
        for d, v in equity:
            w.writerow([d, round(v)] + [round(START_CASH * index[s][d] / index[s][days[0]]) for s in index])


if __name__ == "__main__":
    main()
