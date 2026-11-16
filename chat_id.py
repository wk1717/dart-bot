"""봇한테 아무 메시지나 보내고 실행하면 chat_id 나옴"""
import os

import bot

bot.load_env(bot.ROOT / ".env")
token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit(".env에 TELEGRAM_BOT_TOKEN을 먼저 넣으세요")

res = bot.http_json(f"https://api.telegram.org/bot{token}/getUpdates")
chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
         for u in res.get("result", []) if "message" in u}
if not chats:
    print("받은 메시지가 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
for chat_id, name in chats.items():
    print(f"{name}: TELEGRAM_CHAT_ID={chat_id}")
