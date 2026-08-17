# -*- coding: utf-8 -*-
"""
outreach_agent.py — агент исходящего аутрича.

На вход: контакт (telegram/email), данные тизера и путь к PDF. Делает две вещи:
  1) ПИШЕТ персональное холодное сообщение владельцу с предложением о покупке бизнеса
     (через Claude, тон уважительный и деловой, со ссылкой на индикативную оценку);
  2) САМ ВЫБИРАЕТ КАНАЛ доставки — приоритет Telegram, затем e-mail (берёт тот, что
     доступен по контакту и настроен в .env) — и отправляет тизер.

Возвращает статус: какой канал использован, отправлено/нет, текст сообщения.
"""
from __future__ import annotations

import claude_client as cc
import telegram_sender as tg
import email_sender as em

SYSTEM = """Ты — партнёр M&A-бутика, пишешь ВЛАДЕЛЬЦУ частной компании первое холодное
сообщение с предложением обсудить продажу/инвестицию в его бизнес. Тон: уважительный,
деловой, без лести и «воды», без давления. Логика: кратко кто мы → что подготовили
индикативную оценку его компании (во вложении) → почему обращаемся именно к нему →
мягкое приглашение к диалогу под NDA. Не раскрывай методику оценки в деталях, не давай
обещаний по цене. 4-6 предложений для мессенджера; для письма — отдельно тема и тело."""


def _compose(content: dict, channel: str) -> dict:
    name = content.get("company_name", "вашей компании")
    ev = (content.get("summary", {}).get("key_figures", {}) or {}).get("ev_range", "")
    pos = content.get("cover", {}).get("positioning", "")
    user = (f"Компания контакта: {name}.\nПозиционирование: {pos}.\n"
            f"Индикативный диапазон оценки: {ev}.\nКанал: {channel}.\n"
            'Верни JSON: {"subject": "<тема для письма или пусто для telegram>", '
            '"message": "<текст сообщения>"}.')
    try:
        return cc.complete_json(SYSTEM, user, max_tokens=900, temperature=0.4)
    except Exception:
        # фолбэк-шаблон, если модель недоступна
        return {"subject": "Индикативная оценка вашего бизнеса",
                "message": (f"Здравствуйте! Мы — отраслевой инвестор. Подготовили для {name} "
                            "индикативную оценку бизнеса (во вложении) и были бы рады обсудить "
                            "возможное партнёрство или приобретение. Если тема актуальна — "
                            "предлагаем короткий разговор под NDA.")}


def _pick_channel(contact: dict) -> str | None:
    """Приоритет: Telegram → e-mail (из доступных по контакту и настроенных в .env)."""
    if contact.get("telegram") and tg.is_enabled():
        return "telegram"
    if contact.get("email") and em.is_enabled():
        return "email"
    return None


def send(contact: dict, content: dict, pdf_path: str, on_progress=None) -> dict:
    """Выбрать канал, написать сообщение, отправить тизер. -> статус-словарь."""
    log = on_progress or (lambda m: None)
    channel = _pick_channel(contact)
    if not channel:
        return {"sent": False, "channel": None,
                "reason": "нет доступного канала (контакт/креды не заданы)"}

    log(f"пишу сообщение для канала {channel}…")
    draft = _compose(content, channel)
    message = draft.get("message", "")
    subject = draft.get("subject") or "Индикативная оценка вашего бизнеса"

    log(f"отправляю через {channel}…")
    try:
        if channel == "telegram":
            tg.send_teaser(contact["telegram"], pdf_path, message)
        else:
            em.send_teaser(contact["email"], subject, message, pdf_path)
    except Exception as e:
        return {"sent": False, "channel": channel, "reason": str(e), "message": message}
    return {"sent": True, "channel": channel, "message": message, "subject": subject}
