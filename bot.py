"""DART 공시 수집 봇

python3 bot.py                    어제~오늘 수집
python3 bot.py 20261113           해당 날짜만
python3 bot.py summary [YYYYMMDD] 요약 전송

날짜는 로컬 시간 기준 (Actions는 TZ=Asia/Seoul)
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
ERROR_ALERT_INTERVAL = 3600  # 초
ENV_KEYS = ["DART_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SUPABASE_URL", "SUPABASE_KEY"]

# report_nm 키워드로 분류, 위에서부터 매칭. 분류 안 돼도 저장은 함
CATEGORIES = {
    "유상증자": ["유상증자결정"],
    "무상증자": ["무상증자결정"],
    "감자": ["감자결정"],
    "전환사채": ["전환사채권발행결정"],
    "최대주주변경": ["최대주주변경"],
    "횡령·배임": ["횡령", "배임"],
    "자사주취득": ["자기주식취득결정"],
    "공급계약": ["공급계약체결"],
    "거래정지": ["매매거래정지"],
    "상장폐지": ["상장폐지"],
}
LISTED = ("Y", "K")  # 유가, 코스닥


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def check_env():
    load_env(ROOT / ".env")
    missing = [k for k in ENV_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit(f"환경변수 없음: {', '.join(missing)}")


def http_json(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(url, body, timeout=20) as r:
        return json.load(r)


def sb(method, path, body=None, prefer=None, count=False):
    # URL에 /rest/v1/까지 붙여 넣어도 되게
    base = os.environ["SUPABASE_URL"].rstrip("/").removesuffix("/rest/v1")
    url = f"{base}/rest/v1/{path}"
    headers = {"apikey": os.environ["SUPABASE_KEY"], "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if count:
                return int(r.headers["Content-Range"].rsplit("/", 1)[1])
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {e.code} {path.split('?')[0]}: {e.read()[:300].decode(errors='replace')}")
    return json.loads(raw) if raw else None


def classify(report_nm):
    for category, words in CATEGORIES.items():
        if any(w in report_nm for w in words):
            return category
    return None


def fetch_disclosures(api_key, bgn, end, corp_cls=None):
    items, page = [], 1
    while True:
        params = {"crtfc_key": api_key, "bgn_de": bgn, "end_de": end, "page_no": page, "page_count": 100}
        if corp_cls:
            params["corp_cls"] = corp_cls
        res = http_json(f"{DART_LIST_URL}?{urllib.parse.urlencode(params)}")
        if res["status"] == "013":  # 데이터 없음 (휴일)
            return items
        if res["status"] != "000":
            raise RuntimeError(f"DART 오류 {res['status']}: {res.get('message')}")
        items += res["list"]
        if page >= res["total_page"]:
            return items
        page += 1


def to_row(item, saved_at):
    return {
        "rcept_no": item["rcept_no"], "rcept_dt": item["rcept_dt"], "corp_code": item["corp_code"],
        "corp_name": item["corp_name"], "stock_code": item["stock_code"], "corp_cls": item["corp_cls"],
        "report_nm": item["report_nm"], "category": classify(item["report_nm"]), "saved_at": saved_at,
    }


def save_rows(rows):
    # 이미 있는 건 무시, 새로 들어간 것만 셈. 중간에 끊겨도 다음 실행 때 채워짐
    saved = 0
    for n in range(0, len(rows), 500):
        saved += len(sb("POST", "disclosures?select=rcept_no", rows[n:n + 500],
                        "resolution=ignore-duplicates,return=representation"))
    return saved


def send_telegram(text):
    res = http_json(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                    {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text})
    if not res.get("ok"):
        raise RuntimeError(f"텔레그램 전송 실패: {res}")


def alert_error(error, now=None):
    now = now or time.time()
    key = hashlib.sha1(error.encode()).hexdigest()
    try:
        rows = sb("GET", f"alerts?select=sent_at&key=eq.{key}")
    except Exception:  # Supabase 장애일 수도 있으니 그냥 보냄
        rows = []
    if rows and now - rows[0]["sent_at"] < ERROR_ALERT_INTERVAL:
        return False
    try:
        send_telegram(f"❌ dart-bot 수집 실패\n{error}")
    except Exception as e:  # 텔레그램도 죽었으면 로그만
        print(f"에러 알림 전송 실패: {e}", file=sys.stderr)
        return False
    try:
        sb("POST", "alerts", {"key": key, "sent_at": now}, "resolution=merge-duplicates")
    except Exception as e:
        print(f"알림 기록 실패: {e}", file=sys.stderr)
    return True


def collect(day=None):
    check_env()
    if day:
        bgn = end = day
    else:
        # 전날 늦게 올라온 공시까지 받으려고 어제부터
        # TODO: 매번 이틀치 다시 받음. DART 일일 한도 걸리면 마지막 접수번호 이후만 받기
        today = date.today()
        bgn, end = (today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")

    started = datetime.now().isoformat(timespec="seconds")
    try:
        rows = [to_row(i, started) for i in fetch_disclosures(os.environ["DART_API_KEY"], bgn, end)
                if i["corp_cls"] in LISTED]
        saved = save_rows(rows)
        sb("POST", "runs", {"started_at": started, "ok": 1, "saved": saved, "error": None})
        print(f"{started} {bgn}~{end}: 새 공시 {saved}건 저장")
        return saved
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        try:
            sb("POST", "runs", {"started_at": started, "ok": 0, "saved": 0, "error": error})
        except Exception as e2:
            print(f"실행 기록 실패: {e2}", file=sys.stderr)
        alert_error(error)
        raise


def build_summary(day):
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    total = sb("HEAD", f"disclosures?rcept_dt=eq.{day}", prefer="count=exact", count=True)
    cats = Counter(r["category"] for r in
                   sb("GET", f"disclosures?select=category&rcept_dt=eq.{day}&category=not.is.null"))
    runs = sb("GET", f"runs?select=ok,error&started_at=like.{iso}*&order=started_at.desc")
    ok = sum(r["ok"] for r in runs)
    last_error = next((r["error"] for r in runs if not r["ok"]), None)

    mark = "✅" if runs and ok == len(runs) else "⚠️"
    lines = [f"{mark} {int(day[4:6])}/{int(day[6:])} 수집 요약",
             f"공시 {total}건 저장" + (f" ({', '.join(f'{c} {n}' for c, n in cats.most_common())})" if cats else ""),
             f"실행 {len(runs)}회 중 성공 {ok}회"]
    if last_error:
        lines.append(f"마지막 에러: {last_error}")
    return "\n".join(lines)


def summary(day=None):
    check_env()
    text = build_summary(day or date.today().strftime("%Y%m%d"))
    send_telegram(text)
    print(text)
    return text


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args[:1] == ["summary"]:
            summary(args[1] if len(args) > 1 else None)
        else:
            collect(args[0] if args else None)
    except Exception as e:
        sys.exit(f"실패: {type(e).__name__}: {e}")
