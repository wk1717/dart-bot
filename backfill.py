"""과거 공시 채우기

python3 backfill.py 20231116 20261113

분류된 공시(유상증자 등)만 저장. 끝난 날짜는 data/backfill_done.txt에 남겨서 다시 돌리면 이어서 함
DART 일일 한도(020) 걸리면 멈춤 → 다음 날 다시 실행
"""
import os
import sys
from datetime import datetime, timedelta

import bot

DONE_PATH = bot.ROOT / "data" / "backfill_done.txt"


def backfill(bgn, end, done_path=DONE_PATH):
    bot.check_env()
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    day, last = datetime.strptime(bgn, "%Y%m%d"), datetime.strptime(end, "%Y%m%d")
    total = 0
    while day <= last:
        d = day.strftime("%Y%m%d")
        day += timedelta(days=1)
        if d in done:
            continue
        try:
            items = [i for cls in bot.LISTED
                     for i in bot.fetch_disclosures(os.environ["DART_API_KEY"], d, d, cls)]
        except RuntimeError as e:
            if "020" in str(e):
                print(f"{d}: DART 일일 한도 초과, 여기서 멈춤")
                return total
            raise
        now = datetime.now().isoformat(timespec="seconds")
        saved = bot.save_rows([bot.to_row(i, now) for i in items if i["corp_cls"] in bot.LISTED and bot.classify(i["report_nm"])])
        with done_path.open("a") as f:
            f.write(d + "\n")
        total += saved
        print(f"{d}: 공시 {len(items)}건 중 {saved}건 저장", flush=True)
    return total


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("사용: python3 backfill.py 시작일 종료일 (YYYYMMDD)")
    print(f"완료: {backfill(sys.argv[1], sys.argv[2])}건 저장")
