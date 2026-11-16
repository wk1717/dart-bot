# dart-bot

DART 유가·코스닥 상장사 공시를 GitHub Actions 크론으로 수집해 Supabase에 쌓는다.
텔레그램으로는 하루 요약과 에러만 보낸다(같은 에러는 1시간에 한 번). 외부 패키지 없이 Python 표준 라이브러리만 쓴다.

## 실행 주기 (평일, KST)
- 수집: 08:00~18:50, 10분마다. 전날 늦게 올라온 공시를 놓치지 않으려고 어제~오늘을 조회한다
- 요약: 19:05. 요약이 안 오면 봇이 죽은 것이다
- GitHub 예약 실행은 몇 분에서 수십 분까지 밀리거나 건너뛸 수 있다

## 설정
1. Supabase 프로젝트를 만들고 SQL Editor에서 `schema.sql` 실행
2. `cp .env.example .env` 후 값 입력 (`SUPABASE_KEY`는 secret 키)
3. 텔레그램에서 봇에게 아무 메시지나 보내고 `python3 chat_id.py` → 나온 값을 `TELEGRAM_CHAT_ID`에
4. 같은 값 다섯 개를 GitHub 저장소 Secrets에 등록

## 직접 실행
```
python3 bot.py                    # 어제~오늘 수집
python3 bot.py 20261113           # 특정 날짜 수집
python3 bot.py summary [YYYYMMDD] # 요약 전송
```
Actions 탭에서 `dart-bot` 워크플로를 수동 실행(collect/summary)할 수도 있다.

## 테스트
`python3 -m unittest discover -s tests -t .`
