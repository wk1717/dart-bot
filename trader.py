"""모의투자 봇 (한국투자증권 모의계좌)

python3 trader.py buy    08:40 실행. 전 거래일 자사주취득 공시 종목을 시장가 매수 → 09:00 시가 체결
python3 trader.py sell   15:20 실행. 산 날 포함 3거래일째인 종목을 시장가 매도 → 종가 체결

규칙은 analyze.py 결과 기준 (자사주취득 3일 평균 +1.76%)
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
ENV_KEYS = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT"]


def check_env():
    bot.check_env()
    missing = [k for k in ENV_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit(f"환경변수 없음: {', '.join(missing)}")


# --- KIS API ---

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
        raise RuntimeError(f"KIS {tr_id} {res.get('msg_cd')}: {res.get('msg1', '').strip()}")
    return res


def account():
    cano, prdt = os.environ["KIS_ACCOUNT"].split("-")
    return {"CANO": cano, "ACNT_PRDT_CD": prdt}


def price(code):
    res = kis("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
              {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    return int(res["output"]["stck_prpr"])  # 장 전에는 전일 종가


def holdings():
    res = kis("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", "VTTC8434R", {
        **account(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
    return {r["pdno"]: int(r["hldg_qty"]) for r in res["output1"] if int(r["hldg_qty"]) > 0}


def order(side, code, qty):
    tr_id = {"buy": "VTTC0012U", "sell": "VTTC0011U"}[side]
    res = kis("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr_id, body={
        **account(), "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"})  # 01 = 시장가
    return res["output"]["ODNO"]


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


def buy(today=None):
    check_env()
    today = today or date.today().strftime("%Y%m%d")
    days = trading_days(today)
    rows = bot.sb("GET", f"disclosures?select=rcept_no,rcept_dt,stock_code,corp_name,corp_cls,report_nm"
                         f"&category=eq.{CATEGORY}&rcept_dt=gt.{days[-2]}&corp_cls=in.({','.join(bot.LISTED)})")
    trades = bot.sb("GET", "trades?select=rcept_no,stock_code,sell_date")
    open_ = [t for t in trades if not t["sell_date"]]
    signals = pick_signals(rows, days, {t["rcept_no"] for t in trades}, {t["stock_code"] for t in open_})
    lines = []
    for r in signals[:max(0, MAX_HOLD - len(open_))]:
        try:
            qty = BUDGET // price(r["stock_code"])
            if qty == 0:
                lines.append(f"건너뜀 {r['corp_name']}: 1주가 {BUDGET:,}원 넘음")
                continue
            odno = order("buy", r["stock_code"], qty)
        except Exception as e:  # 주문 실패는 기록 안 함 → 휴장일이면 다음 거래일에 다시 시도
            lines.append(f"실패 {r['corp_name']}: {e}")
            continue
        bot.sb("POST", "trades", {"rcept_no": r["rcept_no"], "stock_code": r["stock_code"],
                                  "corp_name": r["corp_name"], "qty": qty, "buy_date": today, "buy_odno": odno})
        lines.append(f"매수 {r['corp_name']}({r['stock_code']}) {qty}주")
    report("매수", lines)


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


def report(kind, lines):
    text = f"{'📈' if kind == '매수' else '📉'} {datetime.now():%m/%d} 모의투자 {kind}\n" + ("\n".join(lines) or "대상 없음")
    print(text)
    if lines:
        bot.send_telegram(text)


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd not in (["buy"], ["sell"]):
        sys.exit(__doc__)
    try:
        buy() if cmd == ["buy"] else sell()
    except Exception as e:
        bot.alert_error(f"trader {cmd[0]}: {type(e).__name__}: {e}")
        sys.exit(f"실패: {type(e).__name__}: {e}")
