"""공시 유형별 초과수익률 (이벤트 스터디)

python3 analyze.py

기준가: 공시 다음 거래일 시가
수익률: 1·3·5거래일째 종가 / 기준가 - 1
초과수익률: 종목 수익률 - 같은 기간 지수 수익률 (유가→코스피, 코스닥→코스닥)
결과는 data/event_stats.csv
"""
import csv
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime

import bot
import prices

HORIZONS = [1, 3, 5]
MAX_GAP_DAYS = 7      # 공시 후 첫 거래일이 이보다 늦으면 거래정지로 보고 제외
DELIST_CHECK = 60     # 상장폐지 공시 후 이 거래일 넘게 계속 거래되면 이전상장으로 보고 제외
MIN_SAMPLE = 30


def load_prices(symbol):
    p = prices.PRICE_DIR / f"{symbol}.csv"
    if not p.exists():
        return []
    # 네이버는 거래정지·상폐 종목도 마지막 가격을 복사해 날짜를 채움 → 거래량 0인 날은 버림
    return [(r["date"], float(r["open"]), float(r["close"])) for r in csv.DictReader(p.open())
            if int(r["volume"]) > 0 and float(r["open"]) > 0]


def load_events():
    rows, offset = [], 0
    while True:
        page = bot.sb("GET", "disclosures?select=rcept_no,rcept_dt,stock_code,corp_cls,report_nm,category"
                             f"&category=not.is.null&order=rcept_no&limit=1000&offset={offset}")
        if not page:
            break
        rows += page
        offset += 1000
    # 정정공시 빼고, 같은 날 같은 종목 같은 유형은 하나로
    seen, events = set(), []
    for r in rows:
        key = (r["stock_code"], r["category"], r["rcept_dt"])
        if "정정" in r["report_nm"] or key in seen:
            continue
        seen.add(key)
        events.append(r)
    return events


def event_returns(event, stock, index_by_date):
    """(초과수익률 dict, None) 또는 (None, 제외 사유)"""
    i = next((k for k, (d, _, _) in enumerate(stock) if d > event["rcept_dt"]), None)
    if i is None or i + max(HORIZONS) > len(stock):
        return None, "가격 없음"
    gap = (datetime.strptime(stock[i][0], "%Y%m%d") - datetime.strptime(event["rcept_dt"], "%Y%m%d")).days
    if gap > MAX_GAP_DAYS:
        return None, "거래정지"
    if event["category"] == "상장폐지" and i + DELIST_CHECK < len(stock):
        return None, "이전상장 추정"
    base_date, base_open, _ = stock[i]
    if base_date not in index_by_date:
        return None, "지수 없음"
    out = {}
    for h in HORIZONS:
        d, _, close = stock[i + h - 1]
        if d not in index_by_date:
            return None, "지수 없음"
        stock_ret = close / base_open - 1
        index_ret = index_by_date[d][1] / index_by_date[base_date][0] - 1
        out[h] = stock_ret - index_ret
    return out, None


def summarize(values):
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0
    t = mean / (sd / math.sqrt(n)) if sd else 0
    p = math.erfc(abs(t) / math.sqrt(2))  # 정규근사 양측 p값
    return {"n": n, "mean": mean, "median": statistics.median(values),
            "down": sum(v < 0 for v in values) / n, "t": t, "p": p}


def main():
    bot.check_env()
    index = {cls: {d: (o, c) for d, o, c in load_prices(sym)} for cls, sym in [("Y", "KOSPI"), ("K", "KOSDAQ")]}
    cache, results, dropped = {}, defaultdict(lambda: defaultdict(list)), defaultdict(Counter)
    events = load_events()
    for e in events:
        stock = cache.setdefault(e["stock_code"], load_prices(e["stock_code"]))
        ret, reason = event_returns(e, stock, index[e["corp_cls"]])
        if reason:
            dropped[e["category"]][reason] += 1
            continue
        for h, v in ret.items():
            results[e["category"]][h].append(v)

    out = []
    for cat in sorted(results, key=lambda c: -len(results[c][1])):
        row = {"category": cat}
        for h in HORIZONS:
            s = summarize(results[cat][h])
            row.update({f"{k}_{h}d": v for k, v in s.items()})
        row["dropped"] = dict(dropped[cat])
        out.append(row)

    print(f"이벤트 {len(events)}건 (정정·중복 제외 후)\n")
    print(f"{'유형':<8}{'표본':>6}" + "".join(f"{f'{h}일 평균':>10}{'중앙값':>8}" for h in HORIZONS)
          + f"{'5일 하락비율':>10}{'5일 p값':>9}  제외")
    for r in out:
        flag = "" if r["n_1d"] >= MIN_SAMPLE else " (표본 부족)"
        print(f"{r['category']:<8}{r['n_1d']:>6}"
              + "".join(f"{r[f'mean_{h}d']:>+10.2%}{r[f'median_{h}d']:>+8.2%}" for h in HORIZONS)
              + f"{r['down_5d']:>10.0%}{r['p_5d']:>9.3f}  {r['dropped']}{flag}")

    with (bot.ROOT / "data" / "event_stats.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
