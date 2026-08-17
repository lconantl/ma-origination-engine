# -*- coding: utf-8 -*-
"""
email_sender.py — исходящая рассылка тизеров по электронной почте (SMTP).

Шлёт сопроводительное письмо с вложенным PDF-тизером. Креды из .env:
    SMTP_HOST=smtp.yandex.ru
    SMTP_PORT=465
    SMTP_USER=deals@example.ru
    SMTP_PASSWORD=...
    SMTP_FROM=ООО «Инвестор» <deals@example.ru>
Если креды не заданы — is_enabled()==False, канал «почта» недоступен (агент выберет
другой канал или сообщит, что доставка невозможна).
"""
from __future__ import annotations
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from claude_client import _load_env
_load_env()

HOST = os.environ.get("SMTP_HOST", "")
PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
USER = os.environ.get("SMTP_USER", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM = os.environ.get("SMTP_FROM", "") or USER


def is_enabled() -> bool:
    return bool(HOST and USER and PASSWORD)


def send_teaser(to_email: str, subject: str, body: str, pdf_path: str) -> bool:
    """Отправить письмо с вложенным PDF-тизером. True при успехе."""
    if not is_enabled():
        raise RuntimeError("SMTP не настроен (.env)")
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    data = Path(pdf_path).read_bytes()
    msg.add_attachment(data, maintype="application", subtype="pdf",
                       filename=Path(pdf_path).name)
    ctx = ssl.create_default_context()
    if PORT == 465:                       # SSL
        with smtplib.SMTP_SSL(HOST, PORT, context=ctx, timeout=60) as s:
            s.login(USER, PASSWORD)
            s.send_message(msg)
    else:                                 # STARTTLS (587)
        with smtplib.SMTP(HOST, PORT, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(USER, PASSWORD)
            s.send_message(msg)
    return True


if __name__ == "__main__":
    import sys
    print("Email sender enabled:", is_enabled(), "| host:", HOST or "—")
    if is_enabled() and len(sys.argv) > 2:
        ok = send_teaser(sys.argv[1], "Индикативная оценка вашего бизнеса",
                         "Во вложении — индикативная оценка. Конфиденциально.", sys.argv[2])
        print("sent:", ok)
