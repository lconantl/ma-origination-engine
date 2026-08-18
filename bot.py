# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот M&A-тизеров.

Сценарий: пользователь шлёт ИНН (10/12 цифр) и опц. ссылку на сайт →
бот «Готовлю презентацию…» → присылает PDF-тизер ИЛИ «по ИНН не найдено».

Токен — из .env (TELEGRAM_BOT_TOKEN). Запуск:  .venv/bin/python bot.py
"""
from __future__ import annotations
import asyncio
import logging
import os
import re

# импорт generate_teaser подтягивает claude_client -> .env загружается (override)
import generate_teaser as gt

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          ContextTypes, filters)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("teaser_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

INN_RE = re.compile(r"\b(\d{10}|\d{12})\b")
URL_RE = re.compile(r"(https?://\S+|\bwww\.\S+|\b[a-z0-9-]+\.(?:ru|com|рф|su|io|net|org)\b\S*)", re.I)

WELCOME = (
    "Пришлите *ИНН* компании (10 или 12 цифр). Можно добавить ссылку на сайт — "
    "это улучшит результат.\n\n"
    "Пример:\n`7743195810 https://example.ru`\n\n"
    "В ответ придёт PDF-презентация с оценкой бизнеса, обоснованием и кратким обзором ключевых выводов, целей и сути проекта."
)

_busy: set[int] = set()  # user_id, для которых уже идёт генерация


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(WELCOME)


def _parse(text: str):
    inn_m = INN_RE.search(text or "")
    url_m = URL_RE.search(text or "")
    inn = inn_m.group(1) if inn_m else None
    url = url_m.group(1) if url_m else ""
    if url and not url.lower().startswith("http"):
        url = "https://" + url
    return inn, url


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    inn, url = _parse(msg.text)
    if not inn:
        await msg.reply_markdown(
            "Не вижу ИНН. Пришлите *ИНН* компании — 10 цифр (юрлицо) или 12 (ИП). "
            "Можно с ссылкой на сайт.")
        return

    uid = msg.from_user.id
    if uid in _busy:
        await msg.reply_text("Уже готовлю предыдущий запрос — дождитесь, пожалуйста, результата.")
        return
    _busy.add(uid)
    status = await msg.reply_text(f"Готовлю презентацию по ИНН {inn}. Это займёт 1–2 минуты…")

    async def _say(text):
        try:
            await status.edit_text(text)
        except Exception:
            pass

    try:
        try:
            await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            pdf = await asyncio.to_thread(gt.generate_teaser, inn, url)
        except Exception:
            log.exception("generate_teaser failed")
            await _say("Не удалось сформировать тизер из-за технической ошибки. "
                       "Попробуйте ещё раз чуть позже.")
            return
        if not pdf or not os.path.exists(pdf):
            await _say(f"По ИНН {inn} информации для оценки не найдено.")
            return
        await _say("Готово! Отправляю презентацию…")
        try:
            with open(pdf, "rb") as fh:
                await context.bot.send_document(
                    msg.chat_id, document=fh, filename=os.path.basename(pdf),
                    caption="Инвестиционный тизер. Оценка индикативная, по публичным данным.",
                    read_timeout=180, write_timeout=180, connect_timeout=30)
        except Exception:
            log.exception("send_document failed")
            await msg.reply_text("Тизер готов, но не удалось отправить файл. Попробуйте ещё раз.")
    finally:
        _busy.discard(uid)  # гарантированно снимаем блокировку (даже при отмене)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    # щедрые таймауты: аплоад PDF может идти дольше дефолтных 5с (была ошибка TimedOut)
    app = (Application.builder().token(TOKEN)
           .read_timeout(60).write_timeout(180).connect_timeout(30).pool_timeout(30)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
