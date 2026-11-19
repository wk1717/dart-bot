"""자사주취득 공시를 매입 규모별로 나눠서 초과수익률 비교

python3 buyback.py

매입 비율 = 취득예정 보통주 수 / 상장주식수
DART 상세(tsstkAqDecsn)와 상장주식수(KIS 현재가 조회)는 data/에 캐시
"""
import csv
import json

import analyze
import bot
import trader

DETAIL_PATH = bot.ROOT / "data" / "buyback_detail.json"
SHARES_PATH = bot.ROOT / "data" / "shares.json"
STATS_PATH = bot.ROOT / "data" / "buyback_stats.csv"  # app.py에서 읽음
H = 3  # 봇이 3거래일 들고 있음


def cached(path, keys, fetch):
    data = json.loads(path.read_text()) if path.exists() else {}
    for n, k in enumerate(k for k in keys if k not in data):
        try:
            data[k] = fetch(k)
        except Exception as e:
            print(f"건너뜀 {k}: {e}")
            data[k] = None
        if n % 50 == 49:
            path.write_text(json.dumps(data))  # 끊겨도 이어서
    path.write_text(json.dumps(data))
    return data


def dart_detail(corp_code):
    return trader.dart_buyback(corp_code, "20231101", "20261130")


def listed_shares(code):
    # 지금 상장주식수라 3년 전 공시는 그 사이 증자·감자만큼 어긋남. 정확히 하려면 DART 주식총수현황
    return trader.quote(code)[1]


def main():
    trader.check_env()
    events = [e for e in analyze.load_events() if e["category"] == "자사주취득"]
    corp = {}
    offset = 0
    while True:
        page = bot.sb("GET", f"disclosures?select=rcept_no,corp_code&category=eq.자사주취득"
                             f"&order=rcept_no&limit=1000&offset={offset}")
        if not page:
            break
        corp.update((r["rcept_no"], r["corp_code"]) for r in page)
        offset += 1000
    detail = cached(DETAIL_PATH, sorted({corp[e["rcept_no"]] for e in events}), dart_detail)
    shares = cached(SHARES_PATH, sorted({e["stock_code"] for e in events}), listed_shares)

    index = {cls: {d: (o, c) for d, o, c in analyze.load_prices(s)} for cls, s in [("Y", "KOSPI"), ("K", "KOSDAQ")]}
    rows, missing = [], 0
    for e in events:
        qty = trader.buyback_qty(detail.get(corp[e["rcept_no"]]) or {}, e["rcept_no"])
        total = shares.get(e["stock_code"])
        ret, reason = analyze.event_returns(e, analyze.load_prices(e["stock_code"]), index[e["corp_cls"]])
        if reason:
            continue
        if not qty or not total:
            missing += 1
            continue
        rows.append((qty / total, ret[H]))

    rows.sort()
    n = len(rows)
    print(f"자사주취득 {n}건 (매입 규모 모름 {missing}건 제외), {H}일 초과수익률\n")
    print(f"{'그룹':<6}{'매입 비율':>16}{'표본':>6}{'평균':>9}{'중앙값':>9}{'하락비율':>9}{'p값':>8}")
    out = []
    for name, part in [("하위", rows[:n // 3]), ("중간", rows[n // 3:2 * n // 3]), ("상위", rows[2 * n // 3:])]:
        s = analyze.summarize([r for _, r in part])
        print(f"{name:<6}{part[0][0]:>7.2%} ~ {part[-1][0]:>6.2%}{s['n']:>6}{s['mean']:>+9.2%}{s['median']:>+9.2%}"
              f"{s['down']:>9.0%}{s['p']:>8.3f}")
        out.append({"group": name, "ratio_min": part[0][0], "ratio_max": part[-1][0], **s})
    with STATS_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)


if __name__ == "__main__":
    main()
