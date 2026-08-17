# -*- coding: utf-8 -*-
"""
telegram_sender.py — исходящая рассылка тизеров в Telegram (Bot API).

Отличается от bot.py: bot.py ПРИНИМАЕТ запросы, этот модуль — ИСХОДЯЩАЯ доставка
готового тизера контакту (холодное предложение о покупке бизнеса). Использует тот же
TELEGRAM_BOT_TOKEN. Контакт задаётся как @username или числовой chat_id.

Важно: Telegram-бот может написать пользователю ПЕРВЫМ только если пользователь уже
нажал /start у бота (ограничение платформы). Для холодного аутрича по @username
используется заранее согласованный onboarding или user-аккаунт (MTProto) — здесь
реализована доставка по chat_id/нумерическому id, что покрывает согласованные контакты.
"""
from __future__ import annotations
import os
from pathlib import Path

import requests

from claude_client import _load_env  # повторно используем загрузчик .env (override)
_load_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_API = "https://api.telegram.org/bot{token}/{method}"


def is_enabled() -> bool:
    return bool(TOKEN)


def _post(method: str, **kwargs):
    r = requests.post(_API.format(token=TOKEN, method=method), timeout=60, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def send_message(chat: str | int, text: str) -> dict:
    return _post("sendMessage", data={"chat_id": chat, "text": text,
                                      "parse_mode": "HTML", "disable_web_page_preview": True})


def send_teaser(chat: str | int, pdf_path: str, message: str) -> dict:
    """Отправить сопроводительное сообщение + PDF-тизер контакту."""
    if message:
        send_message(chat, message)
    with open(pdf_path, "rb") as fh:
        return _post("sendDocument", data={"chat_id": chat,
                     "caption": "Индикативная оценка бизнеса · конфиденциально"},
                     files={"document": (Path(pdf_path).name, fh, "application/pdf")})


if __name__ == "__main__":
    import sys
    print("Telegram sender enabled:", is_enabled())
    if is_enabled() and len(sys.argv) > 2:
        print(send_teaser(sys.argv[1], sys.argv[2], "Тест доставки тизера."))
