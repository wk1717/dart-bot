"""주가 받아오기 (네이버 차트, 일봉)

python3 prices.py

분류된 공시가 있는 종목 + 코스피·코스닥 지수를 data/prices/<코드>.csv로 저장
오늘 받은 파일은 건너뜀 → 끊겨도 다시 돌리면 이어서 함
"""
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

import bot

PRICE_DIR = bot.ROOT / "data" / "prices"
CHART_URL = "https://fchart.stock.naver.com/sise.nhn?symbol={}&timeframe=day&count={}&requestType=0"
INDEXES = ["KOSPI", "KOSDAQ"]


def parse_chart(xml):
    # <item data="20261113|214000|216000|205000|209000|12829515" />
    rows = []
    for line in xml.splitlines():
        if "<item" in line:
            d, o, h, l, c, v = line.split('"')[1].split("|")
            rows.append([d, o, h, l, c, v])
    return rows


def fetch_chart(symbol, count=1000):
    req = urllib.request.Request(CHART_URL.format(symbol, count), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse_chart(r.read().decode("euc-kr"))


def fetch_now(codes):
    """종목코드 → 현재가. 네이버 증권 화면이 쓰는 실시간 시세 (일봉 차트는 장중 값이 수십 초씩 늦음)"""
    url = "https://polling.finance.naver.com/api/realtime/domestic/stock/" + ",".join(codes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return {d["itemCode"]: int(d["closePrice"].replace(",", "")) for d in json.load(r)["datas"]}


def stock_codes():
    codes, offset = set(), 0
    while True:
        rows = bot.sb("GET", f"disclosures?select=stock_code&category=not.is.null&order=rcept_no"
                             f"&limit=1000&offset={offset}")
        if not rows:
            return sorted(c for c in codes if c)
        codes.update(r["stock_code"] for r in rows)
        offset += 1000


def save(symbol, rows):
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    with (PRICE_DIR / f"{symbol}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


def fresh(symbol):
    p = PRICE_DIR / f"{symbol}.csv"
    return p.exists() and date.fromtimestamp(p.stat().st_mtime) == date.today()


def main():
    bot.check_env()
    symbols = INDEXES + stock_codes()
    empty = []
    for n, s in enumerate(symbols, 1):
        if fresh(s):
            continue
        rows = fetch_chart(s)
        if not rows:  # 빈 응답으로 기존 파일 덮어쓰지 않음
            empty.append(s)
            continue
        save(s, rows)
        if n % 100 == 0:
            print(f"{datetime.now():%H:%M:%S} {n}/{len(symbols)}", flush=True)
        time.sleep(0.1)
    print(f"완료: {len(symbols)}개, 데이터 없음 {len(empty)}개 {empty[:20]}")


if __name__ == "__main__":
    main()
