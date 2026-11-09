# -*- coding: utf-8 -*-
"""Повторяем ровно шаги 9-10 селф-теста и смотрим, куда деваются апдейты."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "_work2"
if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(exist_ok=True)

API = "http://127.0.0.1:8765/bot{token}/{method}"
TOKEN = "123:TEST"


def call(method, payload=None):
    url = API.format(token=TOKEN, method=method)
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["result"]


mock = subprocess.Popen([sys.executable, str(HERE / "mock_bot_api.py")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
print("мок жив:", mock.poll() is None)

shutil.copy(HERE.parent / "tg_ref.py", WORK / "tg_ref.py")
(WORK / "config.json").write_text(json.dumps({
    "bot_token": TOKEN, "report_chat_id": "999", "timezone": "Europe/Samara",
    "schedule_time": "21:00",
    "channels": [{"chat_id": "-100111", "username": "chan111", "enabled": True}],
}), encoding="utf-8")

env = dict(os.environ)
env["TG_API_BASE"] = API
env["TG_BOT_TOKEN"] = TOKEN

logf = open(WORK / "bot.log", "w", encoding="utf-8")
bot = subprocess.Popen([sys.executable, "tg_ref.py", "bot"], cwd=WORK, env=env,
                       stdout=logf, stderr=subprocess.STDOUT)
time.sleep(3)

for uid, upd in [
    (900, {"update_id": 900, "message": {"chat": {"id": 777, "type": "private"}, "text": "/daily"}}),
    (901, {"update_id": 901, "callback_query": {"id": "cb1", "data": "stats",
             "message": {"chat": {"id": 777, "type": "private"}, "text": ""}}}),
    (902, {"update_id": 902, "message": {"chat": {"id": 777, "type": "private"}, "text": "/status"}}),
    (903, {"update_id": 903, "message": {"chat": {"id": 777, "type": "private"}, "text": "/restart"}}),
]:
    call("__push_update", {"update": upd})
    time.sleep(3)
    dump = call("__dump")
    print(f"после push {uid}: в очереди {len(dump['updates'])}, отправлено сообщений {dump['sent']}")

print("\n=== отправлено ботом ===")
for m in call("__sent"):
    print("-", m.get("text", "")[:120].replace("\n", " | "))

logf.flush()
print("\n=== лог бота ===")
print((WORK / "bot.log").read_text(encoding="utf-8")[:2000])

bot.terminate()
mock.terminate()
