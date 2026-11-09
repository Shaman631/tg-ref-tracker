# -*- coding: utf-8 -*-
"""Сквозная проверка tg_ref.py против мока Telegram API."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "tg_ref.py"
WORK = HERE / "_work"
API = "http://127.0.0.1:8765/bot{token}/{method}"
TOKEN = "123:TEST"

if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
shutil.copy(SRC, WORK / "tg_ref.py")

env = dict(os.environ)
env["TG_API_BASE"] = API
env["TG_BOT_TOKEN"] = TOKEN

mock = subprocess.Popen([sys.executable, str(HERE / "mock_bot_api.py")],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(1.2)


def run(args, expect=0):
    proc = subprocess.run([sys.executable, "tg_ref.py", *args], cwd=WORK, env=env,
                          capture_output=True, text=True, encoding="utf-8")
    print(f"$ tg_ref.py {' '.join(args)}")
    print((proc.stdout + proc.stderr).strip())
    if proc.returncode != expect:
        print(f"!! код возврата {proc.returncode}, ожидался {expect}")
        sys.exit(1)
    return proc.stdout


def mock_call(method, payload=None):
    url = API.format(token=TOKEN, method=method)
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["result"]


def wait_for_sent(count, timeout=25.0):
    """Ждём, пока бот отправит нужное число сообщений (вместо фиксированной паузы)."""
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        sent = mock_call("__sent")
        last = len(sent)
        if last >= count:
            return sent
        time.sleep(0.4)
    raise AssertionError(f"бот не отправил сообщений: ждали {count}, пришло {last}")


try:
    run(["init"])
    run(["config", "set", "chat", "999888"])
    run(["channels", "add", "-100111"])
    run(["channels", "add", "-100222"])
    run(["channels", "list"])
    run(["setup"])

    print("\n=== 1. создаём ссылки дня ===")
    run(["links", "create"])
    run(["links", "list"])

    print("\n=== 2. прирост = 0, статистика собрана ===")
    run(["stats", "--json"])

    print("\n=== 3. имитируем 10 подписчиков по каждой ссылке ===")
    mock_call("__add_joins", {"n": 10})
    out = run(["stats", "--json"])
    data = json.loads(out[out.index("{"):])
    assert data["total_new"] == 20, f"ожидалось 20, получено {data['total_new']}"
    assert all(c["new_joins"] == 10 for c in data["channels"]), data

    print("\n=== 4. повторный запуск в тот же день: прирост не задваивается ===")
    mock_call("__add_joins", {"n": 5})
    out = run(["stats", "--json"])
    data = json.loads(out[out.index("{"):])
    assert data["total_new"] == 30, f"ожидалось 30 (20+5+5), получено {data['total_new']}"
    assert all(c["new_joins"] == 15 for c in data["channels"]), data

    print("\n=== 5. повторное создание ссылок не дублирует ===")
    run(["links", "create"])
    rows = mock_call("__add_joins", {"n": 0})
    links = mock_call("getUpdates")
    print("\n=== 6. ежедневный отчёт уходит в чат ===")
    run(["daily"])
    sent = mock_call("__sent")
    print("отправлено сообщений:", len(sent))
    assert len(sent) == 1, f"ожидался 1 отчёт, отправлено {len(sent)}"

    print("\n=== 6a. повторный запуск в тот же день не дублирует отчёт ===")
    run(["daily", "--if-due"])
    sent = mock_call("__sent")
    assert len(sent) == 1, f"отчёт продублирован: {len(sent)}"

    print("\n=== 6b. диагностика окружения ===")
    run(["health"])

    print("\n=== 7. сохранённый отчёт пересылается повторно ===")
    run(["report"])
    sent = mock_call("__sent")
    assert len(sent) == 2, f"повторный отчёт не отправлен: {len(sent)}"

    print("\n=== 8. бот-режим: процесс стартует и пишет pid-файл ===")
    bot = subprocess.Popen([sys.executable, "tg_ref.py", "bot"], cwd=WORK, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8")
    time.sleep(4)
    pid_file = WORK / ".bot.pid"
    assert pid_file.exists(), "pid-файл не создан"
    assert bot.poll() is None, "бот завершился неожиданно"
    print("pid-файл:", pid_file.read_text().strip())
    if os.name == "posix":
        os.kill(bot.pid, signal.SIGTERM)
        try:
            bot.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot.kill()
        time.sleep(1)
        assert not pid_file.exists(), "pid-файл не удалён после SIGTERM"
        print("graceful-остановка по SIGTERM: pid-файл удалён — корректно")
    else:
        bot.terminate()
        try:
            bot.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot.kill()
        print("Windows: SIGTERM не доставляется, проверка graceful-остановки пропущена")

    print("\n=== 9. управление с телефона: команда /daily и кнопка ===")
    bot_log = open(WORK / "bot_phone.log", "w", encoding="utf-8")
    phone = subprocess.Popen([sys.executable, "tg_ref.py", "bot"], cwd=WORK, env=env,
                              stdout=bot_log, stderr=subprocess.STDOUT)
    time.sleep(3)
    before = len(mock_call("__sent"))

    # 1) команда /daily с телефона
    mock_call("__push_update", {"update": {"update_id": 900, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/daily"}}})
    sent = wait_for_sent(before + 1)
    reply = sent[-1]
    assert "Итого за день" in reply["text"], reply["text"]
    assert reply.get("reply_markup"), "нет инлайн-кнопок в ответе"
    print("команда /daily: отчёт с кнопками отправлен ✅")

    # 2) нажатие инлайн-кнопки «Отчёт»
    mock_call("__push_update", {"update": {"update_id": 901, "callback_query": {
        "id": "cb1", "data": "stats",
        "message": {"chat": {"id": 777, "type": "private"}, "text": ""}}}})
    sent = wait_for_sent(before + 2)
    assert "Итого за день" in sent[-1]["text"], sent[-1]["text"]
    print("нажатие кнопки «Отчёт»: отчёт отправлен ✅")

    print("\n=== 10. /status и /restart с телефона ===")
    mock_call("__push_update", {"update": {"update_id": 902, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/status"}}})
    sent = wait_for_sent(before + 3)
    assert "Состояние трекера" in sent[-1]["text"], sent[-1]["text"]
    print("/status: сводка отправлена ✅")

    mock_call("__push_update", {"update": {"update_id": 903, "message": {
        "chat": {"id": 777, "type": "private"}, "text": "/restart"}}})
    sent = wait_for_sent(before + 4)
    assert "Перезапускаю бота" in sent[-1]["text"], sent[-1]["text"]
    try:
        phone.wait(timeout=20)
        print("/restart: бот корректно завершился, его поднимет watchdog ✅")
    except subprocess.TimeoutExpired:
        phone.kill()
        raise AssertionError("бот не завершился после /restart")

    print("\n✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
finally:
    mock.terminate()
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
