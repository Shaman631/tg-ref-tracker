# -*- coding: utf-8 -*-
"""Проверка PHP-версии: воркер, команды Telegram, веб-страница, состояние."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "_work"
API = "http://127.0.0.1:8765"
TOKEN = "123:TEST"

if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)


def mock_call(method, payload=None):
    url = f"{API}/bot{TOKEN}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["result"]


def wait_for(predicate, timeout=25.0, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception:
            pass
        time.sleep(step)
    return None


env = dict(os.environ)
env.update({
    "BOT_TOKEN": TOKEN,
    "TELEGRAM_API": API,
    "CHANNELS": "@chan111,@chan222",
    "CHAT_ID": "999",
    "SCHEDULE_TIME": "21:00",
    "TZ": "Europe/Samara",
    "STATE_FILE": str(WORK / "state.json"),
    "WEB_KEY": "testkey",
    "PORT": "8792",
})

mock = subprocess.Popen([sys.executable, str(HERE / "mock_bot_api.py")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)

worker = subprocess.Popen(["php", "worker.php"], cwd=HERE, env=env,
                          stdout=open(WORK / "worker.log", "w", encoding="utf-8"),
                          stderr=subprocess.STDOUT)
web = subprocess.Popen(["php", "-S", "127.0.0.1:8792", "index.php"], cwd=HERE, env=env,
                       stdout=open(WORK / "web.log", "w", encoding="utf-8"),
                       stderr=subprocess.STDOUT)

try:
    print("=== 1. воркер запустился ===")
    got = wait_for(lambda: mock_call("__sent") if mock_call("__sent") else None, timeout=10)
    time.sleep(3)
    assert worker.poll() is None, "воркер упал:\n" + (WORK / "worker.log").read_text(encoding="utf-8")
    print("воркер жив ✅")

    print("\n=== 2. команда /daily с телефона ===")
    mock_call("__push_update", {"update": {"update_id": 10, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/daily"}}})
    sent = wait_for(lambda: (mock_call("__sent") if any("Реферальные ссылки" in m.get("text", "")
                                                    for m in mock_call("__sent")) else None))
    assert sent, "отчёт не пришёл:\n" + (WORK / "worker.log").read_text(encoding="utf-8")
    report = [m for m in sent if "Реферальные ссылки" in m.get("text", "")][-1]["text"]
    print(report.replace("\n", " | ")[:300])
    assert "Итого за день: +0" in report, report
    assert "http" in report, report
    print("ссылки созданы, отчёт отправлен ✅")

    print("\n=== 3. приход подписчиков и пересчёт ===")
    mock_call("__add_joins", {"n": 10})
    mock_call("__push_update", {"update": {"update_id": 11, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/daily"}}})
    sent = wait_for(lambda: (mock_call("__sent") if any("Итого за день: +20" in m.get("text", "")
                                                     for m in mock_call("__sent")) else None))
    assert sent, "прирост не посчитан:\n" + (WORK / "worker.log").read_text(encoding="utf-8")
    last = [m for m in sent if "Итого за день: +20" in m.get("text", "")][-1]["text"]
    assert "Пришло по ссылке сегодня: <b>10</b>" in last, last
    print("прирост посчитан: по 10 с каждого из 2 каналов ✅")

    print("\n=== 4. веб-страница ===")

    def http(path, method="GET", cookie=""):
        headers = {"Cookie": cookie} if cookie else {}
        req = urllib.request.Request(f"http://127.0.0.1:8792{path}", method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    wait_for(lambda: http("/health")[0] == 200, timeout=15)
    code, body = http("/health")
    health = json.loads(body)
    assert code == 200 and health["ok"], health
    assert health["channels"] == 2, health
    assert health["worker_seen"], "воркер не отметил пульс"
    print(f"/health: каналов {health['channels']}, воркер {health['worker_seen']} ✅")

    code, _ = http("/")
    assert code == 403, f"без ключа должно быть 403, получено {code}"
    print("без ключа — 403 ✅")

    code, body = http("/?key=testkey")
    assert code == 200 and "Создать ссылки" in body, code
    print("страница с ключом открывается ✅")

    code, body = http("/run", method="POST", cookie="tgref_key=testkey")
    result = json.loads(body)
    assert code == 200 and result.get("ok"), result
    assert "Итого за день" in result["report"], result
    print("/run: полный цикл выполнен ✅")

    code, body = http("/links", cookie="tgref_key=testkey")
    links = json.loads(body)["links"]
    assert len(links) == 2 and links[0]["link"], links
    print(f"/links: {links[0]['link']} ✅")

    print("\n=== 5. состояние на диске ===")
    state = json.loads((WORK / "state.json").read_text(encoding="utf-8"))
    assert state["days"], state
    assert len(state["channels"]) == 2, state
    print(f"state.json: каналов {len(state['channels'])}, отчёт {list(state['days'])[0]} ✅")

    print("\n=== 6. команда /webkey ===")
    mock_call("__push_update", {"update": {"update_id": 12, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/webkey"}}})
    sent = wait_for(lambda: (mock_call("__sent") if any("Ключ доступа" in m.get("text", "")
                                                     for m in mock_call("__sent")) else None))
    assert sent, "/webkey не ответил"
    key_msg = [m for m in sent if "Ключ доступа" in m.get("text", "")][-1]["text"]
    assert "testkey" in key_msg, key_msg
    print("/webkey: ключ отправлен ✅")

    print("\n=== 7. настройки без переменных окружения (config.local.php) ===")
    # отдельная копия приложения, где настройки лежат в файле — как на шаред-хостинге
    local = WORK / "local"
    local.mkdir(exist_ok=True)
    for f in ("index.php", "worker.php", "lib.php", "cron.php"):
        shutil.copy(HERE / f, local / f)
    (local / "config.local.php").write_text(
        "<?php return ['BOT_TOKEN' => '%s', 'CHAT_ID' => '999', 'CHANNELS' => '@chan111',"
        " 'SCHEDULE_TIME' => '21:00', 'TZ' => 'Europe/Samara',"
        " 'STATE_FILE' => __DIR__ . '/state.json'];" % TOKEN,
        encoding="utf-8")
    lenv = dict(os.environ)
    for k in ("BOT_TOKEN", "CHAT_ID", "CHANNELS", "WEB_KEY", "STATE_FILE", "SCHEDULE_TIME", "TZ"):
        lenv.pop(k, None)
    lenv["TELEGRAM_API"] = API

    proc = subprocess.run(["php", "cron.php", "--force"], cwd=local, env=lenv, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    print(proc.stdout.strip() or proc.stderr.strip())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = json.loads((local / "state.json").read_text(encoding="utf-8"))
    assert len(state["channels"]) == 1, state
    assert "@chan111" in state["channels"], state
    print("config.local.php: канал найден без переменных окружения ✅")

    print("\n✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
finally:
    for p in (worker, web):
        p.terminate()
    mock.terminate()
    time.sleep(1)
    for p in (worker, web):
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
