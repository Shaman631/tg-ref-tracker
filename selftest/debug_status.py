# -*- coding: utf-8 -*-
"""Отладка: запустить бота против мока и отправить ему /status, показав вывод бота."""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
WORK = HERE / "_work"
WORK.mkdir(exist_ok=True)

import json
import urllib.request

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
time.sleep(1)

import shutil
shutil.copy(HERE.parent / "tg_ref.py", WORK / "tg_ref.py")
(WORK / "config.json").write_text(json.dumps({
    "bot_token": TOKEN, "report_chat_id": "999", "timezone": "Europe/Samara",
    "schedule_time": "21:00",
    "channels": [{"chat_id": "-100111", "username": "chan111", "enabled": True}],
}), encoding="utf-8")

env = dict(os.environ)
env["TG_API_BASE"] = API
env["TG_BOT_TOKEN"] = TOKEN

logf = open(WORK / "bot_stdout.log", "w", encoding="utf-8")
bot = subprocess.Popen([sys.executable, "tg_ref.py", "bot"], cwd=WORK, env=env,
                       stdout=logf, stderr=subprocess.STDOUT)
time.sleep(3)

call("__push_update", {"update": {"update_id": 900, "message": {
    "chat": {"id": 777, "type": "private"}, "text": "/status"}}})
time.sleep(4)

logf.flush()
print("=== вывод бота ===")
print((WORK / "bot_stdout.log").read_text(encoding="utf-8"))
print("=== отправлено ботом ===")
for m in call("__sent"):
    print("-", m.get("text", "")[:200].replace("\n", " | "))

bot.terminate()
mock.terminate()
