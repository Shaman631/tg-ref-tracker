#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_ref — трекер подписчиков по реферальным (пригласительным) ссылкам Telegram.

Как это работает:
  1) Бот создаёт в каждом канале пригласительную ссылку на текущий день.
  2) Telegram сам считает, сколько людей пришло по каждой ссылке (join_count).
  3) Каждый вечер скрипт снимает статистику, считает прирост за день
     (сегодняшний join_count минус вчерашний снапшот) и шлёт отчёт в Telegram.

Зависимостей нет — только стандартная библиотека Python 3.9+.

Команды:
  init                 создать config.json с настройками по умолчанию
  setup                проверка токена, прав бота и настроек
  discover             найти каналы, где бот админ (по обновлениям Telegram)
  channels list|add|rm управление списком каналов
  links create         создать ссылки дня (вручную)
  links list           показать созданные ссылки
  stats                снять статистику (--send — отправить отчёт)
  daily                create + stats + отчёт (это запускает планировщик)
  report               повторно отправить сохранённый отчёт за дату
  bot                  включить бот-режим: /start /links /newlinks /stats
  config set ...       токен, chat_id для отчётов, время автосоздания
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
# TG_API_BASE позволяет указать локальный Bot API сервер или прокси:
# TG_API_BASE="http://127.0.0.1:8081/bot{token}/{method}"
API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org/bot{token}/{method}")
# Путь к базе можно переопределить (на Render — на persistent disk: /var/data/reflinks.db)
DB_PATH = Path(os.environ.get("TG_REF_DB") or (BASE_DIR / "reflinks.db"))

DEFAULT_TZ = "Europe/Samara"
FALLBACK_UTC_OFFSET_HOURS = 4  # Самара GMT+4, без перехода на летнее время

DEFAULT_CONFIG = {
    "bot_token": "",
    "report_chat_id": "",
    "backup_chat_id": "",
    "web_key": "",
    "timezone": DEFAULT_TZ,
    "utc_offset": FALLBACK_UTC_OFFSET_HOURS,
    "schedule_time": "21:00",
    "member_limit": 0,        # 0 = без лимита участников на одну ссылку
    "link_expire_hours": 0,   # 0 = ссылка не истекает
    "channels": [],           # [{"chat_id": "...", "title": "...", "username": "...", "enabled": true}]
}


# --------------------------------------------------------------------------- #
#  Утилиты
# --------------------------------------------------------------------------- #

def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"⚠️  {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def now_tz(cfg: dict) -> dt.datetime:
    """Текущее время в таймзоне конфига (с запасным вариантом фиксированного смещения)."""
    name = cfg.get("timezone") or DEFAULT_TZ
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(name))
    except Exception:
        offset = cfg.get("utc_offset", FALLBACK_UTC_OFFSET_HOURS)
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = FALLBACK_UTC_OFFSET_HOURS
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=offset)))


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except json.JSONDecodeError as exc:
            die(f"config.json повреждён: {exc}")
    return apply_env(cfg)


def apply_env(cfg: dict) -> dict:
    """Переменные окружения важнее файла config.json — так настраивается на Render."""
    env = os.environ.get

    token = (env("TG_BOT_TOKEN") or "").strip()
    if token:
        cfg["bot_token"] = token

    chat_id = (env("TG_REPORT_CHAT_ID") or "").strip()
    if chat_id:
        cfg["report_chat_id"] = chat_id

    channels = (env("TG_CHANNELS") or "").strip()
    if channels:
        parsed = []
        for name in channels.replace(";", ",").split(","):
            name = name.strip()
            if not name:
                continue
            if not name.startswith("@") and not name.startswith("-"):
                name = "@" + name
            parsed.append({"chat_id": name, "title": name, "username": name.lstrip("@"),
                           "enabled": True})
        if parsed:
            cfg["channels"] = parsed

    for env_key, cfg_key in (("TG_SCHEDULE_TIME", "schedule_time"),
                             ("TG_TIMEZONE", "timezone"),
                             ("TG_MEMBER_LIMIT", "member_limit"),
                             ("TG_LINK_EXPIRE_HOURS", "link_expire_hours"),
                             ("TG_UTC_OFFSET", "utc_offset"),
                             ("TG_WEB_KEY", "web_key"),
                             ("TG_BACKUP_CHAT_ID", "backup_chat_id")):
        val = env(env_key)
        if val is None or val == "":
            continue
        if cfg_key in ("member_limit", "link_expire_hours", "utc_offset"):
            try:
                cfg[cfg_key] = int(val)
            except ValueError:
                warn(f"{env_key}={val!r} не число — игнорирую")
        else:
            cfg[cfg_key] = val.strip()

    if not cfg.get("report_chat_id"):
        # на Render отчёты можно слать в ту же приватную переписку с ботом
        cfg["report_chat_id"] = cfg.get("report_chat_id") or ""
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def get_token(cfg: dict) -> str:
    token = (os.environ.get("TG_BOT_TOKEN") or cfg.get("bot_token") or "").strip()
    if not token:
        die(
            "Не задан токен бота. Задайте его в config.json (поле bot_token), "
            "через переменную окружения TG_BOT_TOKEN или командой:\n"
            "  python tg_ref.py config set token <ТОКЕН>"
        )
    return token


def token_present(cfg: dict) -> bool:
    return bool((os.environ.get("TG_BOT_TOKEN") or cfg.get("bot_token") or "").strip())


def parse_time_hm(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:.\s]\s*(\d{2})\s*", value or "")
    if not m:
        raise ValueError(f"Неверное время: {value!r}. Ожидается ЧЧ:ММ, например 21:00")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Неверное время: {value!r}")
    return hh, mm


# --------------------------------------------------------------------------- #
#  Telegram Bot API
# --------------------------------------------------------------------------- #

class TelegramError(RuntimeError):
    pass


def api_call(method: str, payload: dict | None = None, token: str | None = None,
             retries: int = 3) -> object:
    """Вызов Bot API. Возвращает result, при ошибке бросает TelegramError."""
    token = token or get_token(load_config())
    url = API_BASE.format(token=token, method=method)
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    last_error = None

    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"ok": False, "description": raw or f"HTTP {exc.code}"}
            if exc.code == 429 and attempt < retries - 1:
                wait = int(data.get("parameters", {}).get("retry_after", 5)) + 1
                warn(f"Telegram просит подождать {wait} с…")
                time.sleep(wait)
                continue
            if exc.code >= 500 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            last_error = data.get("description") or f"HTTP {exc.code}"
            break
        except urllib.error.URLError as exc:
            last_error = f"сеть недоступна: {exc.reason}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            break

        if data.get("ok"):
            return data.get("result")
        last_error = data.get("description") or "неизвестная ошибка"
        if "too many requests" in str(last_error).lower() and attempt < retries - 1:
            time.sleep(5)
            continue
        break

    raise TelegramError(f"{method}: {last_error}")


def get_me() -> dict:
    return api_call("getMe")  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
#  База данных
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         TEXT NOT NULL,
    invite_link     TEXT NOT NULL UNIQUE,
    link_name       TEXT,
    date            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    member_limit    INTEGER DEFAULT 0,
    expire_date     INTEGER,
    join_count      INTEGER DEFAULT 0,
    invite_count    INTEGER DEFAULT 0,
    join_request_count INTEGER DEFAULT 0,
    revoked         INTEGER DEFAULT 0,
    last_checked    TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_chat_date ON links (chat_id, date);

CREATE TABLE IF NOT EXISTS daily_stats (
    date            TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    channel_label   TEXT,
    invite_link     TEXT,
    new_joins       INTEGER DEFAULT 0,
    total_joins     INTEGER DEFAULT 0,
    member_count    INTEGER,
    member_delta    INTEGER,
    updated_at      TEXT,
    PRIMARY KEY (date, chat_id)
);

CREATE TABLE IF NOT EXISTS discovered_chats (
    chat_id     TEXT PRIMARY KEY,
    title       TEXT,
    username    TEXT,
    status      TEXT,
    role        TEXT,
    found_at    TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    date            TEXT PRIMARY KEY,
    links_created_at TEXT,
    report_sent_at  TEXT,
    status          TEXT,
    last_error      TEXT,
    updated_at      TEXT
);
"""


def db() -> sqlite3.Connection:
    parent = DB_PATH.parent
    if parent and not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warn(f"Не удалось создать каталог для базы ({parent}): {exc}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
#  Каналы
# --------------------------------------------------------------------------- #

def configured_channels(cfg: dict, only: list[str] | None = None) -> list[dict]:
    out = []
    for ch in cfg.get("channels", []):
        if not isinstance(ch, dict):
            continue
        if not ch.get("enabled", True):
            continue
        cid = str(ch.get("chat_id", "")).strip()
        if not cid:
            continue
        if only and cid not in only and str(ch.get("username", "")) not in only:
            continue
        out.append(ch)
    return out


def channel_label(ch: dict, chat: dict | None = None) -> str:
    if chat:
        if chat.get("username"):
            return "@" + chat["username"]
        if chat.get("title"):
            return chat["title"]
    if ch.get("username"):
        return "@" + str(ch["username"]).lstrip("@")
    return ch.get("title") or str(ch.get("chat_id"))


def get_chat(chat_id: str) -> dict:
    try:
        return api_call("getChat", {"chat_id": chat_id})  # type: ignore[return-value]
    except TelegramError as exc:
        return {"_error": str(exc)}


def bot_status_in_channel(chat_id: str, bot_id: int) -> tuple[str, str]:
    """Возвращает (status, rights) для бота в канале."""
    try:
        res = api_call("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    except TelegramError as exc:
        return "unknown", str(exc)
    status = res.get("status", "unknown")  # type: ignore[union-attr]
    rights = res.get("can_invite_users", False)  # type: ignore[union-attr]
    return status, ("can_invite_users=yes" if rights else "can_invite_users=NO")


# --------------------------------------------------------------------------- #
#  Ссылки
# --------------------------------------------------------------------------- #

def make_link_name(chat: dict, cfg: dict, date_str: str) -> str:
    stamp = date_str.replace("-", "")
    raw = (chat.get("username") or chat.get("title") or str(chat.get("id")) or "ch")
    raw = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw)).strip("_")[:20]
    return f"ref_{stamp}_{raw}"[:32]


def create_links(cfg: dict, date_str: str, only: list[str] | None = None,
                 force: bool = False, verbose: bool = True) -> list[dict]:
    """Создаёт ссылку дня для каждого канала. Возвращает список созданных/найденных."""
    conn = db()
    created: list[dict] = []
    me = get_me()
    bot_id = me["id"]

    for ch in configured_channels(cfg, only):
        chat_id = str(ch["chat_id"])
        chat = get_chat(chat_id)
        if "_error" in chat:
            warn(f"{chat_id}: не удалось получить канал — {chat['_error']}")
            continue

        row = conn.execute(
            "SELECT * FROM links WHERE chat_id=? AND date=? AND revoked=0", (chat_id, date_str)
        ).fetchone()
        if row and not force:
            if verbose:
                log(f"↩️  {channel_label(ch, chat)}: ссылка дня уже есть — {row['invite_link']}")
            created.append(dict(row))
            continue

        status, rights = bot_status_in_channel(chat_id, bot_id)
        if status != "administrator":
            warn(f"{channel_label(ch, chat)}: бот не админ (status={status}). Пропускаю.")
            continue
        if rights != "can_invite_users=yes":
            warn(f"{channel_label(ch, chat)}: нет права «Приглашать пользователей». Пропускаю.")
            continue

        name = make_link_name(chat, cfg, date_str)
        payload: dict = {"chat_id": chat_id, "name": name}
        limit = int(cfg.get("member_limit") or 0)
        if limit > 0:
            payload["member_limit"] = limit
        hours = int(cfg.get("link_expire_hours") or 0)
        if hours > 0:
            payload["expire_date"] = int(time.time()) + hours * 3600

        try:
            inv = api_call("createChatInviteLink", payload)
        except TelegramError as exc:
            warn(f"{channel_label(ch, chat)}: не удалось создать ссылку — {exc}")
            continue

        invite_link = inv["invite_link"]  # type: ignore[index]
        conn.execute(
            "INSERT OR REPLACE INTO links (chat_id, invite_link, link_name, date, created_at,"
            " member_limit, expire_date, join_count, invite_count, join_request_count, revoked)"
            " VALUES (?,?,?,?,?,?,?,0,0,0,0)",
            (chat_id, invite_link, name, date_str, dt.datetime.now().isoformat(timespec="seconds"),
             limit, payload.get("expire_date")),
        )
        conn.commit()
        created.append({"chat_id": chat_id, "invite_link": invite_link, "name": name,
                        "date": date_str, "channel_label": channel_label(ch, chat)})
        if verbose:
            log(f"✅ {channel_label(ch, chat)}: {invite_link}")

    conn.close()
    return created


def link_for(conn: sqlite3.Connection, chat_id: str, date_str: str):
    return conn.execute(
        "SELECT * FROM links WHERE chat_id=? AND date=? AND revoked=0", (chat_id, date_str)
    ).fetchone()


def latest_link(conn: sqlite3.Connection, chat_id: str):
    return conn.execute(
        "SELECT * FROM links WHERE chat_id=? AND revoked=0 ORDER BY date DESC, id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
#  Статистика и отчёт
# --------------------------------------------------------------------------- #

def collect_stats(cfg: dict, date_str: str) -> dict:
    conn = db()
    channels = configured_channels(cfg)
    rows = []
    total_new = 0

    for ch in channels:
        chat_id = str(ch["chat_id"])
        chat = get_chat(chat_id)
        label = channel_label(ch, chat if "_error" not in chat else None)

        link_row = link_for(conn, chat_id, date_str) or latest_link(conn, chat_id)
        invite_link = link_row["invite_link"] if link_row else None
        total_joins = 0
        new_joins = 0
        prev_snap = 0

        if link_row:
            prev_snap = int(link_row["join_count"] or 0)
            try:
                st = api_call("getChatInviteLinkStats",
                              {"chat_id": chat_id, "invite_link": invite_link})
                total_joins = int(st.get("join_count", 0))  # type: ignore[union-attr]
                new_joins = max(0, total_joins - prev_snap)
                conn.execute(
                    "UPDATE links SET join_count=?, invite_count=?, join_request_count=?,"
                    " last_checked=? WHERE id=?",
                    (total_joins, int(st.get("invite_count", 0)),  # type: ignore[union-attr]
                     int(st.get("join_request_count", 0)),  # type: ignore[union-attr]
                     dt.datetime.now().isoformat(timespec="seconds"), link_row["id"]),
                )
            except TelegramError as exc:
                warn(f"{label}: не удалось получить статистику ссылки — {exc}")

        member_count = None
        member_delta = None
        try:
            member_count = int(api_call("getChatMemberCount", {"chat_id": chat_id}))
            prev = conn.execute(
                "SELECT member_count FROM daily_stats WHERE chat_id=? AND date<?"
                " ORDER BY date DESC LIMIT 1", (chat_id, date_str)
            ).fetchone()
            if prev and prev["member_count"] is not None:
                member_delta = member_count - int(prev["member_count"])
        except TelegramError as exc:
            warn(f"{label}: не удалось получить число подписчиков — {exc}")

        # Прирост за дату накапливаем: повторный прогон в тот же день прибавляет
        # только новую дельту к уже сохранённому значению, а не затирает его.
        prev_day = conn.execute(
            "SELECT new_joins FROM daily_stats WHERE date=? AND chat_id=?", (date_str, chat_id)
        ).fetchone()
        already = int(prev_day["new_joins"] or 0) if prev_day else 0
        new_joins_total = already + new_joins

        conn.execute(
            "INSERT INTO daily_stats (date, chat_id, channel_label, invite_link, new_joins,"
            " total_joins, member_count, member_delta, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(date, chat_id) DO UPDATE SET channel_label=excluded.channel_label,"
            " invite_link=excluded.invite_link, new_joins=excluded.new_joins,"
            " total_joins=excluded.total_joins, member_count=excluded.member_count,"
            " member_delta=excluded.member_delta, updated_at=excluded.updated_at",
            (date_str, chat_id, label, invite_link, new_joins_total, total_joins,
             member_count, member_delta, dt.datetime.now().isoformat(timespec="seconds")),
        )
        total_new += new_joins_total
        rows.append({
            "chat_id": chat_id, "label": label, "invite_link": invite_link,
            "new_joins": new_joins_total, "delta_since_last_check": new_joins,
            "total_joins": total_joins,
            "member_count": member_count, "member_delta": member_delta,
            "link_date": link_row["date"] if link_row else None,
        })

    conn.commit()
    conn.close()
    return {"date": date_str, "channels": rows, "total_new": total_new}


def build_report_text(data: dict) -> str:
    e = html.escape
    lines = [f"<b>📊 Реферальные ссылки — {e(data['date'])}</b>"]
    if not data["channels"]:
        lines.append("\nКаналы не настроены. Добавьте их: <code>python tg_ref.py channels add &lt;id&gt;</code>")
        return "\n".join(lines)

    for row in data["channels"]:
        label = e(row["label"])
        lines.append(f"\n<b>{label}</b>")
        if row["invite_link"]:
            link = e(row["invite_link"])
            suffix = ""
            if row.get("link_date") and row["link_date"] != data["date"]:
                suffix = f" (ссылка от {e(row['link_date'])})"
            lines.append(f"• Ссылка: <code>{link}</code>{suffix}")
        else:
            lines.append("• Ссылка дня ещё не создана")
        lines.append(f"• Пришло по ссылке сегодня: <b>{row['new_joins']}</b> (всего по ссылке: {row['total_joins']})")
        if row["member_count"] is not None:
            delta = row["member_delta"]
            tail = f" ({delta:+d} за день)" if delta is not None else ""
            lines.append(f"• Подписчиков в канале: <b>{row['member_count']}</b>{tail}")

    lines.append(f"\n<b>Итого за день: +{data['total_new']}</b> подписчиков по ссылкам")
    return "\n".join(lines)


def build_report_plain(data: dict) -> str:
    lines = [f"Реферальные ссылки — {data['date']}"]
    for row in data["channels"]:
        lines.append(f"\n{row['label']}")
        if row["invite_link"]:
            suffix = ""
            if row.get("link_date") and row["link_date"] != data["date"]:
                suffix = f" (ссылка от {row['link_date']})"
            lines.append(f"  ссылка: {row['invite_link']}{suffix}")
        lines.append(f"  сегодня по ссылке: {row['new_joins']} (всего: {row['total_joins']})")
        if row["member_count"] is not None:
            delta = row["member_delta"]
            tail = f" ({delta:+d} за день)" if delta is not None else ""
            lines.append(f"  подписчиков в канале: {row['member_count']}{tail}")
    lines.append(f"\nИтого за день: +{data['total_new']}")
    return "\n".join(lines)


def send_message(chat_id: str, text: str, html_mode: bool = True,
                 reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    api_call("sendMessage", payload)


def resolve_report_chat(cfg: dict, override: str | None = None) -> str:
    cid = (override or cfg.get("report_chat_id") or "").strip()
    if not cid:
        die("Не указан чат для отчётов. Задайте report_chat_id в config.json "
            "или выполните: python tg_ref.py config set chat <chat_id>")
    return cid


# --------------------------------------------------------------------------- #
#  Бот-режим (long polling)
# --------------------------------------------------------------------------- #

def bot_commands_help() -> str:
    return (
        "Команды бота:\n"
        "/daily — полный цикл: создать ссылки + собрать статистику + отчёт\n"
        "/stats — собрать и прислать отчёт прямо сейчас\n"
        "/links — ссылки дня по каналам\n"
        "/newlinks — создать ссылки дня вручную\n"
        "/status — состояние системы (аптайм, ближайший запуск, последний отчёт)\n"
        "/restart — перезапустить бота (поднимет watchdog)\n"
        "/channels — список каналов и статус прав\n"
        "/help — эта справка\n\n"
        "Кнопки ниже делают то же самое — можно пользоваться с телефона."
    )


def main_keyboard() -> dict:
    """Инлайн-кнопки — управление трекером с телефона."""
    return {
        "inline_keyboard": [
            [{"text": "🌙 Полный цикл", "callback_data": "daily"},
             {"text": "📊 Отчёт", "callback_data": "stats"}],
            [{"text": "🔗 Ссылки дня", "callback_data": "links"},
             {"text": "🆕 Создать ссылки", "callback_data": "newlinks"}],
        ]
    }


def build_status_text(cfg: dict, state: dict) -> str:
    """Сводка для команды /status — что с системой прямо сейчас."""
    now = now_tz(cfg)
    conn = db()
    last = conn.execute(
        "SELECT * FROM job_runs ORDER BY date DESC LIMIT 1").fetchone()
    links = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
    conn.close()

    hh, mm = parse_time_hm(cfg.get("schedule_time") or "21:00")
    # now — с часовым поясом, поэтому и следующий запуск строим в той же зоне
    today_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    next_run = today_run if now < today_run else today_run + dt.timedelta(days=1)

    started = state.get("started_at")
    uptime = "—"
    if started:
        delta = dt.datetime.now() - started
        mins = int(delta.total_seconds() // 60)
        uptime = f"{mins // 60} ч {mins % 60} мин"

    lines = [
        "<b>Состояние трекера</b>",
        f"• Сейчас: {now.strftime('%d.%m.%Y %H:%M')} ({cfg.get('timezone')})",
        f"• Бот работает: {uptime}",
        f"• Ближайший автозапуск: {next_run.strftime('%d.%m.%Y %H:%M')}",
        f"• Каналов в работе: {len(configured_channels(cfg))}",
        f"• Создано ссылок всего: {links}",
    ]
    if last:
        lines.append(f"• Последняя задача: {last['date']} — {last['status']}, "
                     f"отчёт: {last['report_sent_at'] or 'не отправлен'}")
    lines.append("\n" + bot_commands_help())
    return "\n".join(lines)


def handle_bot_callback(callback: dict, cfg: dict, state: dict) -> None:
    """Обработка нажатий на инлайн-кнопки."""
    data = callback.get("data") or ""
    chat = (callback.get("message") or {}).get("chat", {})
    chat_id = str(chat.get("id"))
    today = now_tz(cfg).date().isoformat()

    try:
        if data == "daily":
            create_links(cfg, today, verbose=False)
            report = collect_stats(cfg, today)
            send_message(chat_id, build_report_text(report), reply_markup=main_keyboard())
        elif data == "stats":
            report = collect_stats(cfg, today)
            send_message(chat_id, build_report_text(report), reply_markup=main_keyboard())
        elif data == "links":
            conn = db()
            rows = []
            for ch in configured_channels(cfg):
                r = link_for(conn, str(ch["chat_id"]), today)
                rows.append(f"• {channel_label(ch)}: {r['invite_link'] if r else '— нет ссылки —'}")
            conn.close()
            send_message(chat_id, f"Ссылки на {today}:\n\n" + "\n".join(rows) if rows else "Каналы не настроены.",
                         reply_markup=main_keyboard())
        elif data == "newlinks":
            created = create_links(cfg, today, verbose=False)
            text_out = "\n".join(f"• {c['channel_label']}: {c['invite_link']}" for c in created)
            send_message(chat_id,
                         f"Ссылки на {today} созданы:\n\n{text_out}" if created else "Новых ссылок нет (уже созданы).",
                         reply_markup=main_keyboard())
        else:
            send_message(chat_id, bot_commands_help())
    except TelegramError as exc:
        try:
            send_message(chat_id, f"Ошибка: {exc}")
        except TelegramError:
            pass
    finally:
        try:
            api_call("answerCallbackQuery", {"callback_query_id": callback.get("id", "")})
        except TelegramError:
            pass


def handle_bot_update(update: dict, cfg: dict, state: dict) -> None:
    conn = db()

    if "my_chat_member" in update:
        mcm = update["my_chat_member"]
        chat = mcm.get("chat", {})
        if chat.get("type") in ("channel", "supergroup"):
            conn.execute(
                "INSERT OR REPLACE INTO discovered_chats (chat_id, title, username, status, role, found_at)"
                " VALUES (?,?,?,?,?,?)",
                (str(chat.get("id")), chat.get("title"), chat.get("username"),
                 mcm.get("new_chat_member", {}).get("status"), "admin", dt.datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        return

    if "callback_query" in update:
        conn.close()
        handle_bot_callback(update["callback_query"], cfg, state)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        conn.close()
        return
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id"))

    # присланный файл бэкапа → восстановление состояния
    doc = msg.get("document") or {}
    if doc.get("file_name", "").endswith("tg_ref_state.json.gz"):
        conn.close()
        try:
            file_info = api_call("getFile", {"file_id": doc["file_id"]})
            blob = api_download((file_info or {}).get("file_path", ""))
            data = json.loads(gzip.decompress(blob).decode("utf-8"))
            n = import_state(data)
            send_message(chat_id, f"✅ Состояние восстановлено, записей: {n}")
        except Exception as exc:
            send_message(chat_id, f"❌ Не удалось восстановить: {exc}")
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        conn.close()
        return

    cmd = text.split()[0].split("@")[0].lower()
    today = now_tz(cfg).date().isoformat()

    try:
        if cmd == "/start":
            send_message(
                chat_id,
                "👋 Привет! Это трекер реферальных ссылок.\n\n"
                f"Ваш chat_id: <code>{chat_id}</code>\n"
                f"Укажите его в config.json (report_chat_id), чтобы получать отчёты сюда.\n\n"
                + bot_commands_help(),
                reply_markup=main_keyboard(),
            )
        elif cmd == "/links":
            rows = []
            conn2 = db()
            for ch in configured_channels(cfg):
                r = link_for(conn2, str(ch["chat_id"]), today)
                rows.append(f"• {channel_label(ch)}: {r['invite_link'] if r else '— нет ссылки —'}")
            conn2.close()
            send_message(chat_id, f"Ссылки на {today}:\n\n" + "\n".join(rows) if rows else "Каналы не настроены.",
                         reply_markup=main_keyboard())
        elif cmd == "/newlinks":
            created = create_links(cfg, today, verbose=False)
            text_out = "\n".join(f"• {c['channel_label']}: {c['invite_link']}" for c in created)
            send_message(chat_id, f"Ссылки на {today} созданы:\n\n{text_out}" if created else "Новых ссылок нет (уже созданы).",
                         reply_markup=main_keyboard())
        elif cmd == "/stats":
            data = collect_stats(cfg, today)
            send_message(chat_id, build_report_text(data), reply_markup=main_keyboard())
        elif cmd == "/daily":
            create_links(cfg, today, verbose=False)
            data = collect_stats(cfg, today)
            send_message(chat_id, build_report_text(data), reply_markup=main_keyboard())
        elif cmd == "/status":
            send_message(chat_id, build_status_text(cfg, state), reply_markup=main_keyboard())
        elif cmd == "/restart":
            send_message(chat_id,
                         "🔄 Перезапускаю бота. Watchdog поднимет его в течение минуты.\n"
                         "Команды, отправленные пока бот был выключен, Telegram хранит "
                         "и выполнит сразу после запуска — до 24 часов.",
                         reply_markup=main_keyboard())
            state["restart"] = True
        elif cmd == "/channels":
            lines = []
            for ch in configured_channels(cfg):
                status, rights = bot_status_in_channel(str(ch["chat_id"]), state["bot_id"])
                lines.append(f"• {channel_label(ch)} — {status}, {rights}")
            send_message(chat_id, "Каналы:\n\n" + "\n".join(lines) if lines else "Каналы не настроены.",
                         reply_markup=main_keyboard())
        elif cmd == "/help":
            send_message(chat_id, bot_commands_help(), reply_markup=main_keyboard())
    except TelegramError as exc:
        try:
            send_message(chat_id, f"Ошибка: {exc}")
        except TelegramError:
            pass
    finally:
        conn.close()


def cmd_bot(cfg: dict) -> None:
    me = get_me()
    pid_file = BASE_DIR / ".bot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    log(f"🤖 Бот @{me['username']} запущен (pid {os.getpid()}). Ctrl+C — остановить.")

    # SIGTERM/SIGINT должны завершать бота штатно, чтобы pid-файл убрался.
    def _stop(signum, frame):
        raise KeyboardInterrupt

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    offset = None
    state = {"bot_id": me["id"], "started_at": dt.datetime.now(), "restart": False}
    try:
        bot_loop(cfg, state)
    except KeyboardInterrupt:
        log("\nОстановлено.")
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  Команды CLI
# --------------------------------------------------------------------------- #

def cmd_init(cfg: dict) -> None:
    if CONFIG_PATH.exists():
        log(f"config.json уже существует: {CONFIG_PATH}")
    else:
        save_config(cfg)
        log(f"Создан {CONFIG_PATH}")
    conn = db()
    conn.close()
    log(f"Создана база {DB_PATH}")
    log("\nДальше:\n  1) python tg_ref.py config set token <ТОКЕН>\n"
        "  2) добавьте бота в каналы администратором (право «Приглашать пользователей»)\n"
        "  3) python tg_ref.py discover --save   (подхватит каналы автоматически)\n"
        "  4) напишите боту /start и выполните python tg_ref.py config set chat <ваш chat_id>\n"
        "  5) python tg_ref.py daily --send")


def cmd_setup(cfg: dict) -> None:
    log("=== Проверка настроек ===")
    try:
        me = get_me()
        log(f"✅ Токен рабочий: @{me['username']} (id {me['id']})")
    except TelegramError as exc:
        die(f"Токен не работает: {exc}")

    log(f"Таймзона: {cfg.get('timezone')}   Время автосоздания: {cfg.get('schedule_time')}")
    log(f"Чат для отчётов: {cfg.get('report_chat_id') or '— не задан —'}")
    log(f"Каналов в конфиге: {len(configured_channels(cfg))}")

    for ch in configured_channels(cfg):
        chat_id = str(ch["chat_id"])
        chat = get_chat(chat_id)
        if "_error" in chat:
            warn(f"{chat_id}: ошибка getChat — {chat['_error']}")
            continue
        status, rights = bot_status_in_channel(chat_id, me["id"])
        flag = "✅" if status == "administrator" and rights == "can_invite_users=yes" else "❌"
        log(f"{flag} {channel_label(ch, chat)} ({chat_id}) — статус: {status}, {rights}")


def cmd_discover(cfg: dict, save: bool) -> None:
    log("Ищу каналы, где бот администратор… (нужно, чтобы бот был добавлен в каналы)")
    found: dict[str, dict] = {}
    offset = None
    for _ in range(3):
        payload: dict = {"timeout": 3}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = api_call("getUpdates", payload)
        except TelegramError as exc:
            warn(f"getUpdates: {exc}")
            break
        if not updates:
            break
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            for key in ("my_chat_member", "channel_post", "message"):
                if key in upd:
                    ch = upd[key].get("chat", {})
                    if ch.get("type") in ("channel", "supergroup"):
                        found[str(ch.get("id"))] = {
                            "chat_id": str(ch.get("id")),
                            "title": ch.get("title"),
                            "username": ch.get("username"),
                        }

    if not found:
        log("Ничего не найдено. Возможные причины:\n"
            "  • бот ещё не добавлен ни в один канал;\n"
            "  • обновления уже были прочитаны (тогда добавьте каналы вручную: channels add).")
        return

    log("\nНайдены каналы:")
    for cid, info in found.items():
        log(f"  • {info.get('title') or info.get('username') or cid} — {cid}")

    if save:
        existing = {str(c.get("chat_id")): c for c in cfg.get("channels", []) if isinstance(c, dict)}
        for cid, info in found.items():
            if cid not in existing:
                existing[cid] = {"chat_id": cid, "title": info.get("title"),
                                 "username": info.get("username"), "enabled": True}
        cfg["channels"] = list(existing.values())
        save_config(cfg)
        log(f"\n✅ Записано в config.json: {len(existing)} канал(ов).")


def cmd_channels(cfg: dict, args) -> None:
    action = args.action
    if action == "list":
        rows = configured_channels(cfg)
        if not rows:
            log("Список пуст. Добавьте каналы: python tg_ref.py channels add @mychannel")
            return
        for ch in rows:
            log(f"• {channel_label(ch)} ({ch['chat_id']}) — {'вкл' if ch.get('enabled', True) else 'выкл'}")
    elif action == "add":
        cid = args.chat_id
        existing = [c for c in cfg.get("channels", []) if isinstance(c, dict)]
        found = None
        for c in existing:
            if str(c.get("chat_id")) == cid or str(c.get("username", "")).lstrip("@") == str(cid).lstrip("@"):
                found = c
                break
        chat = get_chat(cid)
        title = getattr(args, "title", None) or (chat.get("title") if "_error" not in chat else None)
        username = chat.get("username") if "_error" not in chat else None
        if found:
            found.update({"enabled": True, "title": title or found.get("title"),
                          "username": username or found.get("username")})
            log(f"Обновлён канал {cid}")
        else:
            existing.append({"chat_id": str(cid), "title": title, "username": username, "enabled": True})
            cfg["channels"] = existing
            log(f"✅ Добавлен канал {cid}" + (f" ({title})" if title else ""))
        save_config(cfg)
    elif action == "rm":
        existing = [c for c in cfg.get("channels", []) if isinstance(c, dict)
                    and str(c.get("chat_id")) != args.chat_id
                    and str(c.get("username", "")).lstrip("@") != str(args.chat_id).lstrip("@")]
        removed = len(cfg.get("channels", [])) - len(existing)
        cfg["channels"] = existing
        save_config(cfg)
        log(f"Удалено каналов: {removed}")


def cmd_links(cfg: dict, args) -> None:
    conn = db()
    if args.action == "list":
        rows = conn.execute(
            "SELECT * FROM links ORDER BY date DESC, id DESC LIMIT ?", (args.days,)
        ).fetchall()
        if not rows:
            log("Ссылок пока нет.")
            return
        for r in rows:
            log(f"{r['date']}  {r['chat_id']}  join={r['join_count']}  {r['invite_link']}")
    elif args.action == "create":
        date_str = args.date or now_tz(cfg).date().isoformat()
        only = args.channel or None
        created = create_links(cfg, date_str, only=only, force=args.force)
        log(f"\nГотово: {len(created)} ссылок на {date_str}")
    conn.close()


def cmd_stats(cfg: dict, args) -> None:
    if not token_present(cfg):
        warn("Токен бота не задан — статистику снять нельзя. См. README, шаг 3.")
        return
    date_str = args.date or now_tz(cfg).date().isoformat()
    data = collect_stats(cfg, date_str)
    if args.json:
        log(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        log(build_report_plain(data))
    if args.send:
        chat_id = resolve_report_chat(cfg, args.chat)
        send_message(chat_id, build_report_text(data))
        log(f"\nОтчёт отправлен в чат {chat_id}")


def mark_job(conn: sqlite3.Connection, date_str: str, status: str,
             links_created_at: str | None = None, report_sent_at: str | None = None,
             last_error: str | None = None) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO job_runs (date, links_created_at, report_sent_at, status, last_error, updated_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET"
        " links_created_at=COALESCE(excluded.links_created_at, job_runs.links_created_at),"
        " report_sent_at=COALESCE(excluded.report_sent_at, job_runs.report_sent_at),"
        " status=excluded.status, last_error=excluded.last_error, updated_at=excluded.updated_at",
        (date_str, links_created_at, report_sent_at, status, last_error, now),
    )
    conn.commit()


def due_dates(cfg: dict, now: dt.datetime) -> list[str]:
    """Даты, по которым ежедневная задача ещё не выполнена и уже наступила."""
    hh, mm = parse_time_hm(cfg.get("schedule_time") or "21:00")
    conn = db()
    out: list[str] = []
    today = now.date()
    yesterday = today - dt.timedelta(days=1)

    row = conn.execute("SELECT report_sent_at FROM job_runs WHERE date=?",
                       (yesterday.isoformat(),)).fetchone()
    if row is None or row["report_sent_at"] is None:
        links = conn.execute("SELECT COUNT(*) AS c FROM links WHERE date=?",
                             (yesterday.isoformat(),)).fetchone()["c"]
        if links:
            out.append(yesterday.isoformat())

    row = conn.execute("SELECT report_sent_at FROM job_runs WHERE date=?",
                       (today.isoformat(),)).fetchone()
    if now.time() >= dt.time(hh, mm) and (row is None or row["report_sent_at"] is None):
        out.append(today.isoformat())

    conn.close()
    return out


def run_daily_job(cfg: dict, date_str: str, args) -> None:
    conn = db()
    log(f"\n=== Ежедневная задача за {date_str} ===")
    try:
        if not args.no_create:
            created = create_links(cfg, date_str, force=args.force, verbose=False)
            if created:
                log(f"Ссылок для {date_str}: {len(created)}")
            mark_job(conn, date_str, "links_created",
                     links_created_at=dt.datetime.now().isoformat(timespec="seconds"))
        data = collect_stats(cfg, date_str)
        log(build_report_plain(data))
        if args.no_send:
            mark_job(conn, date_str, "stats_only")
            return
        chat_id = (args.chat or cfg.get("report_chat_id") or "").strip()
        if not chat_id:
            warn("Чат для отчётов не задан (report_chat_id) — отчёт не отправлен.")
            mark_job(conn, date_str, "no_chat")
            return
        send_message(chat_id, build_report_text(data))
        log(f"Отчёт отправлен в чат {chat_id}")
        mark_job(conn, date_str, "done",
                 report_sent_at=dt.datetime.now().isoformat(timespec="seconds"))
    except TelegramError as exc:
        mark_job(conn, date_str, "error", last_error=str(exc))
        raise
    finally:
        conn.close()


def cmd_daily(cfg: dict, args) -> None:
    if not token_present(cfg):
        warn("Токен бота не задан — ежедневный запуск пропущен. См. README.")
        return
    if not configured_channels(cfg):
        warn("Каналы не настроены — ежедневный запуск пропущен. См. README.")
        return

    now = now_tz(cfg)
    if getattr(args, "if_due", False):
        dates = due_dates(cfg, now)
        if not dates:
            log(f"По расписанию ({cfg.get('schedule_time')}) задача не наступила "
                f"или уже выполнена — выходим.")
            return
    else:
        dates = [args.date or now.date().isoformat()]

    for date_str in dates:
        conn = db()
        row = conn.execute("SELECT report_sent_at FROM job_runs WHERE date=?",
                           (date_str,)).fetchone()
        conn.close()
        if row and row["report_sent_at"] and not getattr(args, "force", False):
            log(f"Отчёт за {date_str} уже отправлен — повторно не слать.")
            continue
        run_daily_job(cfg, date_str, args)


def cmd_health(cfg: dict, args) -> None:
    """Диагностика окружения: что настроено, что не работает."""
    conn = db()
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM job_runs ORDER BY date DESC LIMIT 5").fetchall()]
    links_total = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
    conn.close()

    checks = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "now_local": now_tz(cfg).isoformat(timespec="seconds"),
        "timezone": cfg.get("timezone"),
        "schedule_time": cfg.get("schedule_time"),
        "config_exists": CONFIG_PATH.exists(),
        "db_path": str(DB_PATH),
        "db_writable": os.access(DB_PATH, os.W_OK) if DB_PATH.exists() else False,
        "token_set": token_present(cfg),
        "report_chat_id_set": bool(str(cfg.get("report_chat_id") or "").strip()),
        "channels_count": len(configured_channels(cfg)),
        "links_total": links_total,
        "recent_jobs": recent,
    }
    ok = (checks["token_set"] and checks["report_chat_id_set"]
          and checks["channels_count"] > 0 and checks["db_writable"])
    checks["status"] = "ok" if ok else "needs_setup"

    if getattr(args, "json", False):
        log(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        log("=== Состояние tg_ref ===")
        for key in ("python", "platform", "now_local", "timezone", "schedule_time",
                    "token_set", "report_chat_id_set", "channels_count",
                    "links_total", "db_writable", "status"):
            log(f"  {key}: {checks[key]}")
        if recent:
            log("  последние запуски:")
            for r in recent:
                log(f"    {r['date']} — статус: {r['status']}, отчёт: {r['report_sent_at'] or '—'}")
    if not ok:
        sys.exit(1)


def cmd_report(cfg: dict, args) -> None:
    conn = db()
    date_str = args.date or now_tz(cfg).date().isoformat()
    rows = conn.execute(
        "SELECT * FROM daily_stats WHERE date=?", (date_str,)
    ).fetchall()
    if not rows:
        conn.close()
        die(f"За {date_str} нет сохранённой статистики.")
    total = sum(int(r["new_joins"] or 0) for r in rows)
    channels = []
    for r in rows:
        link_date = None
        if r["invite_link"]:
            lrow = conn.execute(
                "SELECT date FROM links WHERE invite_link=?", (r["invite_link"],)
            ).fetchone()
            link_date = lrow["date"] if lrow else None
        channels.append({
            "label": r["channel_label"] or r["chat_id"],
            "invite_link": r["invite_link"],
            "new_joins": r["new_joins"] or 0,
            "total_joins": r["total_joins"] or 0,
            "member_count": r["member_count"],
            "member_delta": r["member_delta"],
            "link_date": link_date,
        })
    data = {
        "date": date_str,
        "total_new": total,
        "channels": channels,
    }
    conn.close()
    chat_id = resolve_report_chat(cfg, args.chat)
    send_message(chat_id, build_report_text(data))
    log(f"Отчёт за {date_str} отправлен в чат {chat_id}")


def ask_secret(prompt: str) -> str:
    """Ввод с скрытием символов — чтобы токен не светился на экране."""
    try:
        import getpass
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        return input(prompt).strip()


def find_private_chat_id(timeout: int = 90) -> tuple[str | None, str]:
    """Ищет ваш личный чат по команде /start, которую вы отправили боту."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            updates = api_call("getUpdates", {"timeout": 5, "limit": 50}) or []
        except TelegramError:
            return None, ""
        for upd in updates:
            msg = upd.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            if chat.get("type") == "private" and chat.get("id"):
                who = chat.get("first_name") or chat.get("username") or ""
                return str(chat["id"]), who
        time.sleep(1)
    return None, ""


def cmd_wizard(cfg: dict, args) -> None:
    """Пошаговая настройка с терминала: токен вводится со скрытием, всё остальное — по подсказкам."""
    log("╔══════════════════════════════════════════╗")
    log("║   Настройка трекера реферальных ссылок     ║")
    log("╚══════════════════════════════════════════╝")

    # 1. Токен
    token = (os.environ.get("TG_BOT_TOKEN") or cfg.get("bot_token") or "").strip()
    if token:
        log("\n[1/5] Токен уже задан (переменная окружения или config.json)")
    else:
        log("\n[1/5] Токен бота от @BotFather")
        log("      Ввод будет скрыт — символы не отображаются.")
        token = ask_secret("      Вставьте токен и нажмите Enter: ")
        if not token:
            die("Токен не введён")
        cfg["bot_token"] = token
        save_config(cfg)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass

    # 2. Проверка токена
    log("\n[2/5] Проверяю токен…")
    try:
        me = get_me()
    except TelegramError as exc:
        die(f"Токен не работает: {exc}")
    log(f"      ✅ Бот @{me['username']} (id {me['id']})")

    # 3. Каналы
    log("\n[3/5] Каналы")
    log("      Укажите каналы через запятую, например: @my_channel, @second_channel")
    log("      (пробелы не важны; Enter — пропустить и добавить позже)")
    try:
        raw = input("      Каналы: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    names = [n.strip().lstrip("@") for n in raw.replace(";", ",").split(",") if n.strip()]
    for name in names:
        try:
            cmd_channels(cfg, argparse.Namespace(action="add", chat_id=f"@{name}", title=None))
        except Exception as exc:
            warn(f"      Не удалось добавить @{name}: {exc}")
    if not names:
        log("      Каналы не заданы — добавить позже: python tg_ref.py channels add @имя")

    log("      Не забудьте: Управление каналом → Администраторы → добавить бота")
    log("      с правом «Приглашать пользователей».")

    # 4. chat_id
    log("\n[4/5] Куда присылать отчёты")
    chat_id = (cfg.get("report_chat_id") or "").strip()
    if chat_id:
        log(f"      Уже задан: {chat_id}")
    else:
        log("      Напишите боту /start в Telegram — я найду ваш chat_id сам.")
        bot_running = (BASE_DIR / ".bot.pid").exists()
        if bot_running:
            log("      (бот временно останавливается, чтобы не забрать ваше сообщение)")
            try:
                pid = int((BASE_DIR / ".bot.pid").read_text(encoding="utf-8").strip())
                os.kill(pid, 15)
                time.sleep(1)
            except (OSError, ValueError):
                pass
        try:
            found, who = find_private_chat_id(timeout=90)
        finally:
            if bot_running:
                subprocess_start_bot()
        if found:
            cfg["report_chat_id"] = found
            save_config(cfg)
            log(f"      ✅ Нашёл: {found}" + (f" ({who})" if who else ""))
        else:
            log("      ⚠️  Не нашёл сообщение. Напишите боту /start и выполните:")
            log("         python tg_ref.py config set chat")

    # 5. Итог
    log("\n[5/5] Итоговая проверка")
    cmd_setup(cfg)
    log("")
    log("Готово. Первый отчёт прямо сейчас:")
    log("   python tg_ref.py daily --send")
    log("А в Telegram бот ответит на /daily, /stats, /status, /restart.")


def subprocess_start_bot() -> None:
    """Поднимает бота в фоне (используется мастером настройки)."""
    try:
        import subprocess
        logf = open(BASE_DIR / "logs" / "bot.log", "a", encoding="utf-8")
        subprocess.Popen([sys.executable, "tg_ref.py", "bot"], cwd=str(BASE_DIR),
                         stdout=logf, stderr=subprocess.STDOUT)
    except Exception as exc:
        warn(f"Не удалось запустить бота: {exc}")


def export_state() -> dict:
    """Выгружает содержимое базы в словарь (для бэкапа)."""
    conn = db()
    out = {}
    for table in ("links", "daily_stats", "job_runs"):
        out[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    conn.close()
    return out


def import_state(data: dict) -> int:
    """Восстанавливает состояние из бэкапа. Возвращает число записей."""
    conn = db()
    total = 0
    for table in ("links", "daily_stats", "job_runs"):
        rows = data.get(table) or []
        if not rows:
            continue
        cols = list(rows[0].keys())
        placeholders = ",".join("?" * len(cols))
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in cols) for r in rows],
        )
        total += len(rows)
    conn.commit()
    conn.close()
    return total


def api_upload(method: str, fields: dict, filename: str, content: bytes) -> object:
    """Отправляет файл в Telegram (multipart/form-data вручную, без сторонних библиотек)."""
    token = get_token(load_config())
    url = API_BASE.format(token=token, method=method)
    boundary = "----tgref" + os.urandom(12).hex()
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
        f'filename="{filename}"\r\nContent-Type: application/json\r\n\r\n'.encode()
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramError(f"{method}: HTTP {exc.code}") from exc
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data.get("result")


def api_download(file_path: str) -> bytes:
    token = get_token(load_config())
    # база берётся из API_BASE, чтобы работало и с локальным Bot API сервером
    base = API_BASE.split("/bot")[0].rstrip("/")
    url = f"{base}/file/bot{token}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"скачивание файла: HTTP {exc.code}") from exc


def backup_state(cfg: dict) -> str | None:
    """Сохраняет состояние базы файлом в Telegram-чат. Возвращает file_id."""
    chat_id = (cfg.get("backup_chat_id") or cfg.get("report_chat_id") or "").strip()
    if not chat_id or not token_present(cfg):
        return None
    payload = json.dumps(export_state(), ensure_ascii=False).encode("utf-8")
    blob = gzip.compress(payload, 9)
    try:
        res = api_upload("sendDocument", {"chat_id": chat_id, "caption":
                          "Бэкап состояния трекера реферальных ссылок"}, "tg_ref_state.json.gz", blob)
    except TelegramError as exc:
        warn(f"Бэкап не отправлен: {exc}")
        return None
    file_id = (res or {}).get("document", {}).get("file_id")
    if file_id:
        set_backup_pointer(cfg, file_id)
    return file_id


def set_backup_pointer(cfg: dict, file_id: str) -> None:
    """Прячет указатель на последний бэкап в описании профиля бота."""
    try:
        api_call("setMyDescription", {"description": f"tgref:{file_id}"[:140]})
    except TelegramError as exc:
        warn(f"Не удалось сохранить указатель бэкапа: {exc}")


def get_backup_pointer(cfg: dict) -> str | None:
    try:
        me = get_me()
    except TelegramError:
        return None
    desc = me.get("description") or ""
    if desc.startswith("tgref:"):
        return desc[len("tgref:"):].strip() or None
    return None


def restore_state(cfg: dict) -> int:
    """Восстанавливает состояние из последнего бэкапа, если база пустая."""
    conn = db()
    has_rows = conn.execute("SELECT COUNT(*) AS c FROM job_runs").fetchone()["c"]
    conn.close()
    if has_rows:
        return 0
    file_id = get_backup_pointer(cfg)
    if not file_id:
        return 0
    try:
        file_info = api_call("getFile", {"file_id": file_id})
        file_path = (file_info or {}).get("file_path")
        if not file_path:
            return 0
        blob = api_download(file_path)
        data = json.loads(gzip.decompress(blob).decode("utf-8"))
        return import_state(data)
    except (TelegramError, OSError, ValueError, json.JSONDecodeError) as exc:
        warn(f"Восстановление не удалось: {exc}")
        return 0


def cmd_backup(cfg: dict, args) -> None:
    file_id = backup_state(cfg)
    if file_id:
        log(f"✅ Бэкап отправлен, file_id: {file_id}")
    else:
        warn("Бэкап не отправлен: проверьте backup_chat_id / report_chat_id и токен")


def cmd_restore(cfg: dict, args) -> None:
    n = restore_state(cfg)
    if n:
        log(f"✅ Восстановлено записей: {n}")
    else:
        log("Восстанавливать нечего: база не пуста или бэкап не найден.")


def run_daily_guard(cfg: dict, date_str: str, force: bool = False) -> str:
    """Запускает ежедневную задачу, если отчёт за эту дату ещё не уходил."""
    conn = db()
    row = conn.execute("SELECT report_sent_at FROM job_runs WHERE date=?", (date_str,)).fetchone()
    conn.close()
    if row and row["report_sent_at"] and not force:
        return "already_sent"

    args = argparse.Namespace(date=date_str, no_send=False, no_create=False,
                              force=force, chat=None, if_due=False)
    run_daily_job(cfg, date_str, args)
    try:
        backup_state(cfg)
    except Exception as exc:
        warn(f"Бэкап не удался: {exc}")
    return "done"


def bot_loop(cfg: dict, state: dict) -> None:
    """Цикл long polling: читает команды из Telegram."""
    offset = None
    while not state.get("stop"):
        payload: dict = {"timeout": 45}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = api_call("getUpdates", payload)
        except TelegramError as exc:
            warn(f"Ошибка long polling: {exc}")
            time.sleep(5)
            continue
        for upd in updates or []:
            offset = int(upd["update_id"]) + 1
            try:
                handle_bot_update(upd, cfg, state)
            except Exception as exc:  # не роняем бота из-за одной команды
                warn(f"Ошибка обработки апдейта: {exc}")
        if state.get("restart"):
            log("Перезапуск по команде /restart")
            return


def scheduler_loop(cfg: dict, stop_event: threading.Event) -> None:
    """Внутренний планировщик: сам решает, наступило ли время ежедневной задачи."""
    while not stop_event.is_set():
        try:
            for date_str in due_dates(cfg, now_tz(cfg)):
                log(f"Планировщик: задача за {date_str} наступила")
                run_daily_guard(cfg, date_str)
        except Exception as exc:
            warn(f"Ошибка планировщика: {exc}")
        stop_event.wait(60)


WEB_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Трекер реферальных ссылок</title>
<style>
  body{font-family:-apple-system,Roboto,"Segoe UI",sans-serif;background:#0f1720;color:#e6edf3;
       margin:0;padding:20px;max-width:520px;margin-inline:auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:#8b98a5;font-size:13px;margin-bottom:18px}
  button{display:block;width:100%;padding:16px;margin-bottom:12px;border:0;border-radius:12px;
         font-size:16px;font-weight:600;background:#2563eb;color:#fff}
  button.alt{background:#1f2937}
  button.green{background:#059669}
  #out{margin-top:16px;padding:14px;border-radius:12px;background:#111c26;font-size:14px;
       white-space:pre-wrap;word-break:break-word;display:none}
  a{color:#60a5fa}
  .foot{margin-top:22px;font-size:12px;color:#6b7a88}
</style></head><body>
<h1>Трекер реферальных ссылок</h1>
<div class="sub">Управление с телефона</div>
<button class="green" onclick="post('/api/run')">🌙 Полный цикл — создать ссылки и прислать отчёт</button>
<button onclick="post('/api/stats')">📊 Отчёт прямо сейчас</button>
<button class="alt" onclick="post('/api/links')">🔗 Ссылки дня</button>
<button class="alt" onclick="post('/api/status')">ℹ️ Состояние системы</button>
<div id="out"></div>
<div class="foot">Отчёты также приходят в Telegram. В боте доступны команды
<a href="#">/daily</a>, <a href="#">/stats</a>, <a href="#">/status</a>, <a href="#">/restart</a>.</div>
<script>
async function post(url){
  const box=document.getElementById('out'); box.style.display='block'; box.textContent='Выполняю…';
  try{
    const r=await fetch(url,{method:'POST'});
    const t=await r.text();
    let data; try{data=JSON.parse(t)}catch(e){data={result:t}}
    box.textContent = data.report || data.result || data.error || t;
  }catch(e){ box.textContent='Ошибка: '+e; }
}
</script>
</body></html>"""

KEY_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ключ доступа</title>
<style>body{font-family:-apple-system,Roboto,sans-serif;background:#0f1720;color:#e6edf3;
padding:24px;max-width:420px;margin-inline:auto}input,button{width:100%;padding:14px;
border-radius:12px;border:0;font-size:16px;margin-top:10px}button{background:#2563eb;color:#fff;
font-weight:600;margin-top:16px}</style></head><body>
<h3>Введите ключ доступа</h3>
<form method="get"><input type="password" name="key" placeholder="ключ" autofocus>
<button>Открыть</button></form></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "tg-ref/1.0"
    cfg: dict = {}
    key: str = ""
    state: dict = {}
    stop_event: threading.Event = threading.Event()

    def log_message(self, *args):  # тише логи Render
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
               extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict) -> None:
        self._reply(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _authorized(self, query: dict) -> bool:
        if not self.key:
            return True
        cookie = self.headers.get("Cookie") or ""
        if f"tgref_key={self.key}" in cookie:
            return True
        given = (query.get("key") or [""])[0]
        return bool(given) and secrets.compare_digest(given, self.key)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            return self._json(200, health_payload(self.cfg))

        if path == "/" and (query.get("key") or [""])[0]:
            if not self._authorized(query):
                return self._reply(401, KEY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return self._reply(302, b"", "text/plain",
                               {"Set-Cookie": f"tgref_key={self.key}; Path=/; Max-Age=31536000"})

        if not self._authorized(query):
            return self._reply(401, KEY_PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/":
            return self._reply(200, WEB_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/links":
            return self._json(200, {"links": today_links(self.cfg)})
        if path == "/api/status":
            return self._json(200, {"result": build_status_text(self.cfg, self.state)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if not self._authorized(query):
            return self._json(403, {"error": "нужен ключ доступа"})

        try:
            if path == "/api/run":
                date_str = now_tz(self.cfg).date().isoformat()
                state = run_daily_guard(self.cfg, date_str)
                data = collect_stats(self.cfg, date_str)
                return self._json(200, {"ok": state == "done", "state": state,
                                        "report": build_report_text(data)})
            if path == "/api/stats":
                date_str = now_tz(self.cfg).date().isoformat()
                data = collect_stats(self.cfg, date_str)
                chat_id = (self.cfg.get("report_chat_id") or "").strip()
                if chat_id:
                    send_message(chat_id, build_report_text(data))
                return self._json(200, {"ok": True, "report": build_report_text(data)})
            if path == "/api/status":
                return self._json(200, {"result": build_status_text(self.cfg, self.state)})
        except TelegramError as exc:
            return self._json(500, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": "not found"})


def today_links(cfg: dict) -> list[dict]:
    date_str = now_tz(cfg).date().isoformat()
    conn = db()
    out = []
    for ch in configured_channels(cfg):
        row = link_for(conn, str(ch["chat_id"]), date_str)
        out.append({"channel": channel_label(ch), "chat_id": ch["chat_id"],
                    "link": row["invite_link"] if row else None,
                    "joins": row["join_count"] if row else 0})
    conn.close()
    return out


def health_payload(cfg: dict) -> dict:
    try:
        conn = db()
        last = conn.execute("SELECT * FROM job_runs ORDER BY date DESC LIMIT 1").fetchone()
        links = conn.execute("SELECT COUNT(*) AS c FROM links").fetchone()["c"]
        conn.close()
        last_job = dict(last) if last else None
    except Exception as exc:
        last_job, links = None, 0
    return {
        "ok": True,
        "time": now_tz(cfg).isoformat(timespec="seconds"),
        "token_set": token_present(cfg),
        "channels": len(configured_channels(cfg)),
        "links_total": links,
        "last_job": last_job,
    }


def cmd_serve(cfg: dict, args) -> None:
    """Режим Render: HTTP-сервис + бот + планировщик в одном процессе."""
    port = int(os.environ.get("PORT") or 8080)
    key = (cfg.get("web_key") or "").strip()
    if not key:
        key = secrets.token_urlsafe(16)
        cfg["web_key"] = key
        try:
            save_config(cfg)
        except OSError:
            pass
        log(f"⚠️  Ключ доступа сгенерирован: {key}")
        log("   Задайте TG_WEB_KEY в переменных окружения, чтобы он не менялся при редеплое.")

    if not token_present(cfg):
        warn("Токен бота не задан (TG_BOT_TOKEN) — бот и отчёты работать не будут.")

    WebHandler.cfg = cfg
    WebHandler.key = key
    WebHandler.state = {"bot_id": 0, "started_at": dt.datetime.now(), "restart": False}
    WebHandler.stop_event = threading.Event()

    # восстанавливаем состояние из бэкапа, если база пуста (бесплатный тариф: диск эфемерный)
    try:
        n = restore_state(cfg)
        if n:
            log(f"✅ Восстановлено из бэкапа записей: {n}")
    except Exception as exc:
        warn(f"Восстановление не удалось: {exc}")

    threads = []
    if os.environ.get("TG_ENABLE_BOT", "1") != "0" and token_present(cfg):
        try:
            me = get_me()
            WebHandler.state["bot_id"] = me["id"]
            t = threading.Thread(target=bot_loop, args=(cfg, WebHandler.state), daemon=True)
            t.start()
            threads.append(t)
            log(f"🤖 Бот @{me['username']} запущен в фоне")
        except TelegramError as exc:
            warn(f"Бот не запущен: {exc}")
    if os.environ.get("TG_ENABLE_SCHEDULER", "1") != "0":
        t = threading.Thread(target=scheduler_loop, args=(cfg, WebHandler.stop_event), daemon=True)
        t.start()
        threads.append(t)
        log("⏰ Планировщик запущен")

    httpd = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    log(f"🌐 Слушаю 0.0.0.0:{port} — управление: http://localhost:{port}/?key={key}")
    log(f"   Проверка состояния: /health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WebHandler.stop_event.set()
        httpd.server_close()


def cmd_worker(cfg: dict, args) -> None:
    """Режим Render Worker: бот + планировщик без HTTP."""
    if not token_present(cfg):
        die("Токен бота не задан (TG_BOT_TOKEN)")
    try:
        n = restore_state(cfg)
        if n:
            log(f"✅ Восстановлено из бэкапа записей: {n}")
    except Exception as exc:
        warn(f"Восстановление не удалось: {exc}")

    me = get_me()
    state = {"bot_id": me["id"], "started_at": dt.datetime.now(), "restart": False}
    stop_event = threading.Event()
    threading.Thread(target=scheduler_loop, args=(cfg, stop_event), daemon=True).start()
    log(f"🤖 Бот @{me['username']} запущен, планировщик работает")
    try:
        bot_loop(cfg, state)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()


def cmd_config(cfg: dict, key: str, value: str) -> None:
    if key == "token":
        value = value.strip() or ask_secret("Токен бота (ввод скрыт): ")
        if not value:
            die("Пустой токен")
        cfg["bot_token"] = value
        save_config(cfg)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
        log("✅ Токен сохранён в config.json (права 600)")
    elif key == "chat":
        value = value.strip() or input("Ваш chat_id (напишите боту /start, он ответит): ").strip()
        if not value:
            die("Пустой chat_id")
        cfg["report_chat_id"] = value
        save_config(cfg)
        log("✅ chat_id для отчётов сохранён")
    elif key == "schedule":
        value = value.strip() or input("Время автосоздания (ЧЧ:ММ): ").strip()
        hh, mm = parse_time_hm(value)
        cfg["schedule_time"] = f"{hh:02d}:{mm:02d}"
        save_config(cfg)
        log(f"✅ Время автосоздания ссылок: {hh:02d}:{mm:02d} ({cfg.get('timezone')})")
    elif key == "timezone":
        value = value.strip() or input("Часовой пояс (Europe/Samara): ").strip()
        cfg["timezone"] = value
        save_config(cfg)
        log(f"✅ Таймзона: {cfg['timezone']}")
    elif key == "member_limit":
        value = value.strip() or input("Лимит участников на ссылку (0 = нет): ").strip()
        cfg["member_limit"] = int(value)
        save_config(cfg)
        log(f"✅ Лимит участников на ссылку: {cfg['member_limit'] or 'без лимита'}")
    elif key == "expire":
        value = value.strip() or input("Срок жизни ссылки в часах (0 = бессрочно): ").strip()
        cfg["link_expire_hours"] = int(value)
        save_config(cfg)
        log(f"✅ Срок жизни ссылки: {cfg['link_expire_hours'] or 'бессрочно'} ч")
    elif key == "utc_offset":
        value = value.strip() or input("Смещение часового пояса (напр. 4): ").strip()
        cfg["utc_offset"] = int(value)
        save_config(cfg)
        log(f"✅ Запасное смещение часового пояса: UTC+{cfg['utc_offset']}")
    else:
        die(f"Неизвестный параметр: {key}")


def cmd_chatid(cfg: dict) -> None:
    log("Ищу ваше последнее сообщение боту…")
    updates = api_call("getUpdates", {"timeout": 3, "limit": 50}) or []
    found = []
    for upd in updates:
        msg = upd.get("message")
        if not msg:
            continue
        chat = msg.get("chat", {})
        found.append((str(chat.get("id")), chat.get("type"),
                      chat.get("first_name") or chat.get("title") or ""))
    if not found:
        log("Не нашёл сообщений. Напишите боту /start в Telegram и повторите.")
        return
    for cid, ctype, who in found:
        log(f"chat_id={cid}  тип={ctype}  {who}")
    log("\nВозьмите ваш (тип=private) и выполните: python tg_ref.py config set chat <chat_id>")


# --------------------------------------------------------------------------- #
#  Точка входа
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tg_ref.py",
        description="Трекер подписчиков по реферальным ссылкам Telegram",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать config.json и базу")
    sub.add_parser("setup", help="проверить токен, права и настройки")
    sub.add_parser("chatid", help="узнать свой chat_id для отчётов")
    sub.add_parser("wizard", help="пошаговая настройка: токен вводится со скрытием")

    d = sub.add_parser("discover", help="найти каналы, где бот админ")
    d.add_argument("--save", action="store_true", help="дописать найденные каналы в config.json")

    ch = sub.add_parser("channels", help="управление каналами")
    chs = ch.add_subparsers(dest="action", required=True)
    chs.add_parser("list")
    a = chs.add_parser("add")
    a.add_argument("chat_id", help="@username или -100xxxxxxxxxx")
    a.add_argument("--title", default=None)
    r = chs.add_parser("rm")
    r.add_argument("chat_id")

    lk = sub.add_parser("links", help="ссылки")
    lks = lk.add_subparsers(dest="action", required=True)
    ll = lks.add_parser("list")
    ll.add_argument("--days", type=int, default=30)
    lc = lks.add_parser("create")
    lc.add_argument("--date", default=None, help="YYYY-MM-DD, по умолчанию сегодня")
    lc.add_argument("--channel", action="append", default=None)
    lc.add_argument("--force", action="store_true", help="пересоздать, даже если ссылка уже есть")

    st = sub.add_parser("stats", help="снять статистику")
    st.add_argument("--date", default=None)
    st.add_argument("--send", action="store_true", help="отправить отчёт в Telegram")
    st.add_argument("--chat", default=None)
    st.add_argument("--json", action="store_true")

    dl = sub.add_parser("daily", help="создать ссылки + собрать статистику + отчёт (для планировщика)")
    dl.add_argument("--date", default=None)
    dl.add_argument("--no-send", action="store_true")
    dl.add_argument("--no-create", action="store_true")
    dl.add_argument("--force", action="store_true")
    dl.add_argument("--chat", default=None)
    dl.add_argument("--if-due", action="store_true",
                    help="выполнить только если задача по расписанию наступила и ещё не выполнена "
                         "(для ежечасного крона на shared-хостинге)")

    hl = sub.add_parser("health", help="диагностика: что настроено, что работает")
    hl.add_argument("--json", action="store_true")

    rp = sub.add_parser("report", help="повторно отправить отчёт за дату")
    rp.add_argument("--date", default=None)
    rp.add_argument("--chat", default=None)

    sub.add_parser("bot", help="бот-режим: /start /links /newlinks /stats")
    sub.add_parser("serve", help="режим Render: HTTP-сервис + бот + планировщик")
    sub.add_parser("worker", help="режим Render Worker: бот + планировщик без HTTP")
    sub.add_parser("backup", help="сохранить состояние базы файлом в Telegram")
    sub.add_parser("restore", help="восстановить состояние из последнего бэкапа")

    cf = sub.add_parser("config", help="настройки")
    cfs = cf.add_subparsers(dest="config_action", required=True)
    config_keys = (("token", "токен бота"), ("chat", "chat_id для отчётов"),
                    ("schedule", "время автосоздания ЧЧ:ММ"), ("timezone", "таймзона"),
                    ("member_limit", "лимит участников на ссылку (0=нет)"),
                    ("expire", "срок жизни ссылки в часах (0=бессрочно)"),
                    ("utc_offset", "смещение часового пояса (запасной вариант, напр. 4)"))
    for key, helptext in config_keys:
        sp = cfs.add_parser(key, help=helptext)
        sp.add_argument("value", nargs="?", default="")
    sp_set = cfs.add_parser("set", help="config set <ключ> <значение> — тот же синтаксис")
    sp_set.add_argument("key", choices=[k for k, _ in config_keys])
    sp_set.add_argument("value", nargs="?", default="")
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config()

    try:
        if args.cmd == "init":
            cmd_init(cfg)
        elif args.cmd == "setup":
            cmd_setup(cfg)
        elif args.cmd == "discover":
            cmd_discover(cfg, args.save)
        elif args.cmd == "channels":
            cmd_channels(cfg, args)
        elif args.cmd == "links":
            cmd_links(cfg, args)
        elif args.cmd == "stats":
            cmd_stats(cfg, args)
        elif args.cmd == "daily":
            cmd_daily(cfg, args)
        elif args.cmd == "health":
            cmd_health(cfg, args)
        elif args.cmd == "report":
            cmd_report(cfg, args)
        elif args.cmd == "bot":
            cmd_bot(cfg)
        elif args.cmd == "serve":
            cmd_serve(cfg, args)
        elif args.cmd == "worker":
            cmd_worker(cfg, args)
        elif args.cmd == "backup":
            cmd_backup(cfg, args)
        elif args.cmd == "restore":
            cmd_restore(cfg, args)
        elif args.cmd == "config":
            key = args.key if args.config_action == "set" else args.config_action
            cmd_config(cfg, key, args.value or "")
        elif args.cmd == "wizard":
            cmd_wizard(cfg, args)
        elif args.cmd == "chatid":
            cmd_chatid(cfg)
    except TelegramError as exc:
        print(f"❌ Telegram API: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
