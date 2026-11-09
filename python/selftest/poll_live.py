# -*- coding: utf-8 -*-
"""Опрос живого сервиса: какая версия кода отвечает."""
import json
import time
import urllib.request

URL = "https://tg-ref-tracker-7msh.onrender.com/health"
for i in range(6):
    try:
        with urllib.request.urlopen(URL, timeout=90) as r:
            d = json.loads(r.read().decode())
        ver = "NEW" if "report_chat_set" in d else "OLD"
        print(f"{i+1}. версия={ver}  time={d.get('time')}  channels={d.get('channels')}  "
              f"token={d.get('token_set')}")
    except Exception as e:
        print(f"{i+1}. ошибка: {e}")
    time.sleep(5)
