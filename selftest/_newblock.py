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
