"""모의투자 봇 (한국투자증권 모의계좌)

python3 trader.py buy    08:40 실행. 전 거래일 자사주취득 공시 종목을 시장가 매수 → 09:00 시가 체결
                         09:05에 한 번 더 실행. 08:40 주문이 거절된 것만 다시 삼 (산 건 trades에 있어서 건너뜀)
                         끝에 체결된 매수가를 buy_price에, 계좌 잔고를 account에 저장 (화면용)
python3 trader.py sell   15:20 실행. 산 날 포함 3거래일째인 종목을 시장가 매도 → 종가 체결
python3 trader.py settle 15:40 실행. 매도 체결가로 수익률 계산해서 알림 (15:20엔 아직 체결 전)
                         끝에 장부(trades)와 KIS 잔고를 맞춰보고 어긋나면 알림 (reconcile만 따로도 됨)

규칙은 analyze.py, buyback.py 결과 기준
- 자사주취득 3일 평균 +1.76%
- 매입 비율(취득예정 주식 / 상장주식) 1% 미만은 효과 없음(-0.14%, p 0.726) → 안 삼. 큰 것부터 삼
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import bot
import prices

BASE = "https://openapivts.koreainvestment.com:29443"  # 모의투자 서버
TOKEN_PATH = bot.ROOT / "data" / "kis_token.json"
CATEGORY = "자사주취득"
BUDGET = 1_000_000   # 종목당
MAX_HOLD = 10
HOLD_DAYS = 3        # 산 날 포함
MIN_RATIO = 0.01     # 매입 비율 하한
ENV_KEYS = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT"]


def check_env():
    bot.check_env()
    missing = [k for k in ENV_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit(f"환경변수 없음: {', '.join(missing)}")


# --- KIS API ---

class Rejected(RuntimeError):
    """KIS가 받아서 거절함 (rt_cd != 0). 이건 주문이 안 나간 게 확실함. 타임아웃·5xx는 나갔는지 모름"""


def _http(method, path, headers, body=None):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode() if body else None,
                                 {"content-type": "application/json", **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"KIS {e.code} {path.split('?')[0]}: {e.read()[:300].decode(errors='replace')}")  # 쿼리에 계좌번호 있음


def token():
    # 토큰은 24시간짜리, 자주 새로 받으면 막힘 → 파일에 두고 재사용 (Actions는 매번 새로 받음)
    if TOKEN_PATH.exists():
        t = json.loads(TOKEN_PATH.read_text())
        if t["expires"] > time.time() + 600:
            return t["token"]
    res = _http("POST", "/oauth2/tokenP", {}, {"grant_type": "client_credentials",
                                              "appkey": os.environ["KIS_APP_KEY"],
                                              "appsecret": os.environ["KIS_APP_SECRET"]})
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps({"token": res["access_token"], "expires": time.time() + res["expires_in"]}))
    return res["access_token"]


def kis(method, path, tr_id, params=None, body=None):
    time.sleep(1)  # 모의투자는 초당 호출 제한이 빡빡함 (0.5초는 EGW00201 걸림)
    if params:
        path += "?" + urllib.parse.urlencode(params)
    res = _http(method, path, {"authorization": f"Bearer {token()}", "appkey": os.environ["KIS_APP_KEY"],
                               "appsecret": os.environ["KIS_APP_SECRET"], "tr_id": tr_id, "custtype": "P"}, body)
    if res.get("rt_cd") != "0":
        raise Rejected(f"KIS {tr_id} {res.get('msg_cd')}: {res.get('msg1', '').strip()}")
    return res


def account():
    cano, prdt = os.environ["KIS_ACCOUNT"].split("-")
    return {"CANO": cano, "ACNT_PRDT_CD": prdt}


def quote(code):
    """(현재가, 상장주식수). 장 전에는 전일 종가"""
    res = kis("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
              {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    return int(res["output"]["stck_prpr"]), int(res["output"]["lstn_stcn"])


def dart_buyback(corp_code, bgn, end):
    """DART 자기주식 취득 결정 상세 → {접수번호: 취득예정 보통주 수(문자열)}"""
    q = urllib.parse.urlencode({"crtfc_key": os.environ["DART_API_KEY"], "corp_code": corp_code,
                                "bgn_de": bgn, "end_de": end})
    res = bot.http_json(f"https://opendart.fss.or.kr/api/tsstkAqDecsn.json?{q}")
    if res["status"] == "013":
        return {}
    if res["status"] != "000":
        raise RuntimeError(f"DART {res['status']} {res.get('message')}")
    return {r["rcept_no"]: r["aqpln_stk_ostk"] for r in res["list"]}


def buyback_qty(detail, rcept_no):
    # 공시 목록과 상세의 접수번호가 다를 때가 있음 (같은 날 다시 낸 것) → 같은 날짜 것으로
    key = rcept_no if rcept_no in detail else next((k for k in detail if k[:8] == rcept_no[:8]), None)
    v = (detail.get(key) or "").replace(",", "")
    return int(v) if v.isdigit() else None


def balance():
    return kis("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", "VTTC8434R", {
        **account(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})


def holdings():
    return {r["pdno"]: int(r["hldg_qty"]) for r in balance()["output1"] if int(r["hldg_qty"]) > 0}


def save_cash():
    """화면용 계좌 잔고. 예수금은 D+2 기준 (결제 전 매매까지 반영)"""
    o = balance()["output2"][0]
    bot.sb("POST", "account", {"id": 1, "cash": int(o["prvs_rcdl_excc_amt"]), "total": int(o["tot_evlu_amt"]),
                               "updated_at": f"{datetime.now():%Y-%m-%d %H:%M}"}, "resolution=merge-duplicates")


def order(side, code, qty):
    tr_id = {"buy": "VTTC0012U", "sell": "VTTC0011U"}[side]
    res = kis("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr_id, body={
        **account(), "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"})  # 01 = 시장가
    return res["output"]["ODNO"]


def fill(code, side, day):
    """그날 그 종목 매수(매도) 체결 (수량, 평균가). 없으면 None
    모의투자는 주문별 목록(output1)이 늘 비어 옴 → 종목·매수매도·날짜로 좁힌 합계(output2)를 씀"""
    res = kis("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "VTTC8001R", {
        **account(), "INQR_STRT_DT": day, "INQR_END_DT": day, "SLL_BUY_DVSN_CD": {"sell": "01", "buy": "02"}[side],
        "INQR_DVSN": "00", "PDNO": code, "CCLD_DVSN": "01", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})  # CCLD_DVSN 01 = 체결된 것만
    qty = int(res["output2"]["tot_ccld_qty"] or 0)
    return (qty, int(res["output2"]["tot_ccld_amt"]) / qty) if qty else None


# --- 규칙 ---

def trading_days(today):
    # 코스피 일봉 날짜 = 거래일. 오늘 것은 장중이라 뺌
    return [r[0] for r in prices.fetch_chart("KOSPI", 10) if r[0] < today]


def pick_signals(rows, days, done, held):
    """오늘이 '공시 다음 거래일'인 공시만. 놓친 날 것은 늦게 사지 않음"""
    prev2, prev = days[-2], days[-1]
    out, seen = [], set(held)
    for r in sorted(rows, key=lambda r: r["rcept_no"]):
        if not prev2 < r["rcept_dt"] <= prev or "정정" in r["report_nm"]:
            continue
        if r["rcept_no"] in done or r["stock_code"] in seen:
            continue
        seen.add(r["stock_code"])
        out.append(r)
    return out


def due_to_sell(buy_date, days):
    # 산 날과 오늘 사이에 거래일이 HOLD_DAYS-2개 있으면 오늘이 HOLD_DAYS번째
    return sum(buy_date < d for d in days) >= HOLD_DAYS - 2


def settle_lines(trades, filled, closed):
    """(DB에 쓸 값, 알림 줄). filled = {접수번호: (매수 체결, 매도 체결)}, closed = 이미 정산된 거래 (누적 계산용)"""
    updates, lines, done = [], [], list(closed)
    for t in trades:
        b, s = filled.get(t["rcept_no"], (None, None))
        if not b or not s:
            lines.append(f"확인 안 됨 {t['corp_name']}: 체결 내역 없음")
            continue
        row = {"buy_price": b[1], "sell_price": s[1], "sell_qty": s[0]}
        updates.append((t["rcept_no"], row))
        done.append(row)
        ret = s[1] / b[1] - 1
        lines.append(f"{t['corp_name']} {s[0]}주 {ret:+.2%} ({(s[1] - b[1]) * s[0]:+,.0f}원)")
    if updates:
        rets = [d["sell_price"] / d["buy_price"] - 1 for d in done]
        pnl = sum((d["sell_price"] - d["buy_price"]) * d["sell_qty"] for d in done)
        lines.append(f"누적 {len(done)}종목 평균 {sum(rets) / len(rets):+.2%}, {pnl:+,.0f}원 (수수료·세금 전)")
    return updates, lines


def fill_buy_prices():
    """보유 중인 거래의 매수 체결가·수량을 매번 다시 채움 (부분 체결 뒤 더 체결될 수 있음). 체결 없으면 다음에"""
    for t in bot.sb("GET", "trades?select=rcept_no,stock_code,corp_name,buy_date&sell_date=is.null"):
        try:
            b = fill(t["stock_code"], "buy", t["buy_date"])
            if b:
                bot.sb("PATCH", f"trades?rcept_no=eq.{t['rcept_no']}", {"qty": b[0], "buy_price": b[1]})
        except Exception as e:  # 화면용이라 실패해도 정산·잔고 저장은 계속
            bot.alert_error(f"trader 매수가 채우기 {t['corp_name']}: {type(e).__name__}: {e}")


def buy(today=None):
    check_env()
    today = today or date.today().strftime("%Y%m%d")
    days = trading_days(today)
    rows = bot.sb("GET", f"disclosures?select=rcept_no,rcept_dt,corp_code,stock_code,corp_name,corp_cls,report_nm"
                         f"&category=eq.{CATEGORY}&rcept_dt=gt.{days[-2]}&corp_cls=in.({','.join(bot.LISTED)})")
    trades = bot.sb("GET", "trades?select=rcept_no,stock_code,sell_date")
    open_ = [t for t in trades if not t["sell_date"]]
    signals = pick_signals(rows, days, {t["rcept_no"] for t in trades}, {t["stock_code"] for t in open_})
    in_account = holdings()  # 장부엔 없는데 계좌에 있는 종목은 안 삼 (장부 쓰기가 실패했던 경우 등)
    lines, ranked = [], []
    for r in signals:
        try:
            qty = buyback_qty(dart_buyback(r["corp_code"], r["rcept_dt"], r["rcept_dt"]), r["rcept_no"])
            px, shares = quote(r["stock_code"])
        except Exception as e:
            lines.append(f"실패 {r['corp_name']}: {e}")
            continue
        if not qty:
            lines.append(f"건너뜀 {r['corp_name']}: 매입 규모 모름")
        elif qty / shares < MIN_RATIO:
            lines.append(f"건너뜀 {r['corp_name']}: 매입 비율 {qty / shares:.2%} (1% 미만)")
        else:
            ranked.append((qty / shares, px, r))
    ranked.sort(key=lambda x: -x[0])  # 크게 사는 것부터
    for ratio, px, r in ranked[:max(0, MAX_HOLD - len(open_))]:
        qty = BUDGET // px
        if qty == 0:
            lines.append(f"건너뜀 {r['corp_name']}: 1주가 {BUDGET:,}원 넘음")
            continue
        if r["stock_code"] in in_account:
            lines.append(f"건너뜀 {r['corp_name']}: 장부에 없는데 계좌에 있음")
            continue
        # 장부에 먼저 쓰고 주문 → 주문 뒤 장부 쓰기가 실패해도 09:05 재실행이 같은 주문을 또 내지 않음
        key = f"trades?rcept_no=eq.{r['rcept_no']}"
        try:
            bot.sb("POST", "trades", {"rcept_no": r["rcept_no"], "stock_code": r["stock_code"],
                                      "corp_name": r["corp_name"], "qty": qty, "buy_date": today})
        except Exception as e:  # 장부에 못 쓰면 주문도 안 냄
            lines.append(f"실패 {r['corp_name']}: 장부 쓰기 {e}")
            continue
        try:
            odno = order("buy", r["stock_code"], qty)
        except Rejected as e:  # 확실히 안 나감 → 지움. 휴장일이면 다음 거래일에 다시 시도
            bot.sb("DELETE", key)
            lines.append(f"실패 {r['corp_name']}: {e}")
            continue
        except Exception as e:  # 나갔는지 모름 → 장부에 남겨서 다시 안 삼. 정산 때 대사에서 걸림
            lines.append(f"확인 필요 {r['corp_name']}: 주문 결과 모름 ({e})")
            continue
        bot.sb("PATCH", key, {"buy_odno": odno})
        lines.append(f"매수 {r['corp_name']}({r['stock_code']}) {qty}주, 매입 비율 {ratio:.2%}")
    report("매수", lines)
    fill_buy_prices()
    save_cash()


def sell(today=None):
    check_env()
    today = today or date.today().strftime("%Y%m%d")
    days = trading_days(today)
    held = holdings()
    lines = []
    for t in bot.sb("GET", "trades?select=rcept_no,stock_code,corp_name,buy_date&sell_date=is.null"):
        if not due_to_sell(t["buy_date"], days):
            continue
        qty = held.get(t["stock_code"], 0)  # 부분 체결도 있으니 실제 잔고 기준
        try:
            odno = order("sell", t["stock_code"], qty) if qty else None
        except Exception as e:
            lines.append(f"실패 {t['corp_name']}: {e}")
            continue
        bot.sb("PATCH", f"trades?rcept_no=eq.{t['rcept_no']}", {"sell_date": today, "sell_odno": odno})
        lines.append(f"매도 {t['corp_name']}({t['stock_code']}) {qty}주" if qty else f"정리 {t['corp_name']}: 잔고 없음")
    report("매도", lines)


def settle(today=None):
    check_env()
    today = today or date.today().strftime("%Y%m%d")
    fill_buy_prices()  # 09:05 뒤 재시도·추가 체결분
    trades = bot.sb("GET", "trades?select=rcept_no,corp_name,stock_code,buy_date,sell_date"
                           "&sell_odno=not.is.null&sell_price=is.null")
    if trades:
        closed = bot.sb("GET", "trades?select=buy_price,sell_price,sell_qty&sell_price=not.is.null")
        filled, ok, failed = {}, [], []
        for t in trades:  # 한 종목 조회가 실패해도 나머지는 정산. 실패한 건 내일 다시
            try:
                filled[t["rcept_no"]] = (fill(t["stock_code"], "buy", t["buy_date"]),
                                         fill(t["stock_code"], "sell", t["sell_date"]))
                ok.append(t)
            except Exception as e:
                failed.append(f"실패 {t['corp_name']}: {e}")
        updates, lines = settle_lines(ok, filled, closed)
        for rcept_no, row in updates:
            bot.sb("PATCH", f"trades?rcept_no=eq.{rcept_no}", row)
        report("수익", failed + lines)
    else:
        print("정산할 거래 없음")
    save_cash()  # 정산 끝난 뒤에. 잔고 저장이 실패해도 정산은 됨
    reconcile()


def reconcile():
    """장부(trades 보유 중)와 KIS 잔고의 종목·수량 비교. 어긋나면 알림. 매도 체결(15:30) 뒤에 돌려야 맞음"""
    check_env()
    book = {t["stock_code"]: (t["corp_name"], t["qty"] or 0)
            for t in bot.sb("GET", "trades?select=stock_code,corp_name,qty&sell_date=is.null")}
    real = holdings()
    lines = []
    for code in sorted(set(book) | set(real)):
        name, want = book.get(code, (code, 0))
        if want != real.get(code, 0):
            lines.append(f"{name}({code}) 장부 {want:,}주 · 계좌 {real.get(code, 0):,}주")
    if lines:
        report("대사 불일치", lines)
    else:
        print(f"대사 일치: {len(book)}종목")


def report(kind, lines):
    text = f"{ {'매수': '📈', '매도': '📉', '대사 불일치': '⚠️'}.get(kind, '💰')} {datetime.now():%m/%d} 모의투자 {kind}\n" + ("\n".join(lines) or "대상 없음")
    print(text)
    if lines:
        bot.send_telegram(text)


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    run = {"buy": buy, "sell": sell, "settle": settle, "reconcile": reconcile}.get(cmd[0] if cmd else "")
    if not run:
        sys.exit(__doc__)
    try:
        run()
    except Exception as e:
        bot.alert_error(f"trader {cmd[0]}: {type(e).__name__}: {e}")
        sys.exit(f"실패: {type(e).__name__}: {e}")
