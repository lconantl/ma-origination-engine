# -*- coding: utf-8 -*-
"""
run_origination.py — полный цикл холодного M&A-онбординга за один проход.

Вход:  ИНН + контакт (telegram/email) + сайт.
Цикл:  данные (СПАРК/ГИР БО/реестры) → оценка по дереву (Claude) → тизер уровня Big3
       (PDF) → агент пишет персональное предложение и сам выбирает канал → отправка.
Выход: тизер доставлен владельцу. Время цикла — минуты вместо рабочего дня.

CLI:
  python run_origination.py <ИНН> --tg @owner_handle --site https://site.ru
  python run_origination.py <ИНН> --email owner@company.ru --site https://site.ru
"""
from __future__ import annotations
import argparse
import sys

import generate_teaser as gt
import outreach_agent as oa


def run(inn: str, contact: dict, url: str = "", on_progress=None) -> dict:
    log = on_progress or (lambda m: print("·", m))
    pdf, content = gt.generate_teaser(inn, url, on_progress=log, with_content=True)
    if not pdf:
        return {"ok": False, "stage": "data", "reason": f"по ИНН {inn} данных не найдено"}
    status = oa.send(contact, content, pdf, on_progress=log)
    return {"ok": status.get("sent", False), "stage": "outreach",
            "pdf": pdf, "channel": status.get("channel"),
            "message": status.get("message"), "reason": status.get("reason")}


def main():
    ap = argparse.ArgumentParser(description="Полный цикл M&A-аутрича по ИНН")
    ap.add_argument("inn")
    ap.add_argument("--tg", dest="telegram", default="", help="@username или chat_id")
    ap.add_argument("--email", default="", help="e-mail контакта")
    ap.add_argument("--site", default="", help="сайт компании (опц., улучшает тизер)")
    a = ap.parse_args()
    contact = {k: v for k, v in (("telegram", a.telegram), ("email", a.email)) if v}
    if not contact:
        sys.exit("Укажите контакт: --tg @handle или --email addr@host")
    res = run(a.inn, contact, a.site)
    print("\nИТОГ:", res)


if __name__ == "__main__":
    main()
