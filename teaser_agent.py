# -*- coding: utf-8 -*-
"""
teaser_agent.py — оркестратор тизера: ИНН + сайт -> единый бандл входов + схема
контента по teaser_content_spec.md. Контент (текст/оценку/нарратив) заполняет
АГЕНТ (Claude), рассуждая по бандлу + методике; рендер -> Presenton/python-pptx.

Поток:
  inputs = collect_inputs(inn, website_url)     # все данные из наших модулей
  # агент: запускает market.queries через WebSearch -> заполняет market_brief
  # агент: применяет valuation_methodology.md -> оценка (low/base/high) + обоснование
  content = teaser_schema()                      # агент заполняет секции
  charts  = chart_builder.build_charts({...})    # графики из точных данных
  -> Presenton (JSON + шаблон бренда) -> PPTX/PDF

Этот модуль НЕ считает оценку сам (это делает агент по методике) — он собирает
данные, считает вспомогательные метрики и даёт схему/чек-лист.
"""
from __future__ import annotations

import data_fetcher as df
import comparables as cmp
import market_research as mr

try:
    import website_fetcher as wf
except Exception:
    wf = None
try:
    import image_search as imgs
except Exception:
    imgs = None

# ОКВЭД-префикс -> sector (для comparables/market/benchmark)
_OKVED_SECTOR = {
    "58": "it", "61": "it", "62": "it", "63": "it", "47": "retail", "46": "wholesale",
    "56": "food_service", "41": "construction", "42": "construction", "43": "construction",
    "49": "transport_logistics", "50": "transport_logistics", "52": "transport_logistics",
    "86": "healthcare", "21": "pharma", "85": "education", "68": "real_estate",
    "01": "agro", "02": "agro", "03": "agro", "55": "real_estate",
    "06": "mining", "05": "mining", "07": "mining", "08": "mining",
    "64": "financial", "65": "financial", "66": "financial",
}
for _n in (10, 11, 13, 14, 15, 16, 17, 18, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33):
    _OKVED_SECTOR[f"{_n:02d}"] = "manufacturing"


def _sector(okved):
    if not okved:
        return "default"
    return _OKVED_SECTOR.get(str(okved).split(".")[0].zfill(2), "default")


def collect_inputs(inn: str, website_url: str = "") -> dict:
    """Собирает ВСЕ доступные данные по компании для тизера (без оценки и без веб-поиска)."""
    inn = str(inn).strip()
    ci = df.get_company_info(inn)
    fin = df.get_financials(inn)
    risk = df.get_risk_flags(inn)
    affil = df.get_affiliates(inn)
    scale = df.get_scale_signals(inn)
    okved = ci.get("okved_code")
    sector = _sector(okved)

    site = None
    if website_url and wf is not None:
        try:
            site = wf.fetch_site(website_url)
        except Exception as e:
            site = {"error": str(e)}

    comps = cmp.find_comparables(sector, n=6)
    market_plan = mr.research_plan(sector, okved_name=ci.get("okved_name") or "",
                                   products=(site or {}).get("title") or "")

    # ФОТО: поиск по бренду (имя из реестра) — основной путь (надёжнее антибота на сайте).
    # Возвращаем КАНДИДАТОВ (url); агент скачивает и отбирает зрением нужные.
    photos = {"candidates": [], "query": None, "note": None}
    if imgs is not None:
        brand = imgs.brand_query(ci.get("name_short") or "", okved_name=ci.get("okved_name") or "")
        photos["query"] = brand
        try:
            photos["candidates"] = imgs.search_images(brand, n=15,
                                                      brand_boost=(ci.get("name_short") or "").split('"')[1].lower()
                                                      if '"' in (ci.get("name_short") or "") else "")
        except Exception as e:
            photos["note"] = f"поиск фото не удался: {e}"
    # бонус: изображения прямо с сайта, если он отдался (SSR/Playwright)
    if site and site.get("images"):
        photos["from_website"] = site["images"][:10]

    return {
        "inn": inn, "sector": sector,
        "company_info": ci, "financials": fin, "risk_flags": risk,
        "affiliates": affil, "scale_signals": scale,
        "benchmark": df.get_benchmark(okved), "macro": df.get_macro(),
        "website": site, "photos": photos, "comparables": comps,
        "market_research_plan": market_plan,
        "data_quality": {
            "company": ci.get("data_quality"), "financials": fin.get("data_quality"),
            "risk": risk.get("data_quality"),
            "website": "ok" if (site and not site.get("error")) else (site or {}).get("error", "нет сайта"),
        },
    }


def teaser_schema() -> dict:
    """Структура контента тизера (секции из teaser_content_spec.md). Заполняет агент."""
    return {
        "cover": {"positioning": None, "subtitle": "Инвестиционная возможность", "date": None},
        "summary": {"stake_offered": None, "one_liner": None, "highlights": [],
                    "key_figures": {"revenue": None, "ebitda": None, "ev_range": None}},
        "company": {"founded": None, "location": None, "business_model": None,
                    "team_size": None, "brands": [], "exclusives": [],
                    "deal_perimeter": []},  # список ИНН/ИП группы
        "market": {"size": None, "cagr": None, "forecast": None, "key_players": [],
                   "tailwinds": [], "risks": [], "sources": []},
        "products": {"range": [], "channels": [], "clients": None},
        "financials": {"years": {}, "commentary": None, "forecast": None},
        "valuation": {"method": None, "ev_low": None, "ev_base": None, "ev_high": None,
                      "justification": None, "comparables": [],
                      "disclaimer": None, "upside_drivers": []},
        "contacts": {"advisor": None, "cta": None, "confidentiality": None},
        "charts": [],   # из chart_builder
        "images": [],   # из website (vision-ранжирование агентом)
    }


# Готовые формулировки (то, что просил пользователь) — агент подставляет в valuation
DISCLAIMER = (
    "Оценка построена на публичной отчётности из государственных реестров (ФНС / ГИР БО) "
    "и носит индикативный, заведомо консервативный характер: публичная отчётность не "
    "отражает нематериальные активы (бренд, клиентскую базу, контракты, технологии) и "
    "потенциал роста. Доступ к управленческой отчётности обычно позволяет уточнить "
    "диапазон оценки. Документ не является офертой; информация конфиденциальна."
)
UPSIDE_CHECKLIST = [
    "Управленческий P&L (реальная прибыль и обороты)",
    "Портфель контрактов / заказов",
    "Данные по клиентам и их диверсификации",
    "Прогноз / бизнес-план развития",
    "Нематериальные активы: бренд, ТЗ, эксклюзивы, IP",
]


def agent_workflow_note() -> str:
    return (
        "ШАГИ АГЕНТА после collect_inputs:\n"
        "1. Периметр: проверь affiliates (ИП/др. ООО собственников) -> консолидируй (методика §11.1).\n"
        "2. Рынок: выполни market_research_plan.queries через WebSearch -> заполни market{}.\n"
        "3. Оценка: пройди valuation_methodology.md (дерево) -> ev_low/base/high + justification.\n"
        "   Подставь DISCLAIMER и UPSIDE_CHECKLIST в valuation.\n"
        "4. Фото: image_search.download_images(photos['candidates']) -> посмотри ЗРЕНИЕМ ->\n"
        "   подбери под слайды (обложка=брендовый баннер, продукт, команда). Сайт-фото — бонус.\n"
        "5. Графики: chart_builder.build_charts({financials, market, comps, segments}).\n"
        "6. Заполни teaser_schema() -> отдай в Presenton (JSON + бренд-шаблон) -> PPTX/PDF.\n"
        "Везде: диапазон, а не точка; помечай data_quality; не выдумывай цифры."
    )


if __name__ == "__main__":
    import sys, json
    inn = sys.argv[1] if len(sys.argv) > 1 else "7743195810"
    url = sys.argv[2] if len(sys.argv) > 2 else "https://karminparts.ru"
    inp = collect_inputs(inn, url)
    print("=== БАНДЛ ВХОДОВ ===")
    print("ИНН:", inp["inn"], "| отрасль:", inp["sector"])
    ci = inp["company_info"]
    print("Компания:", ci.get("name_short"), "| ОКВЭД:", ci.get("okved_code"), "| статус:", ci.get("status"))
    print("Финансы q:", inp["financials"]["data_quality"], "| лет:", len(inp["financials"]["years"]))
    print("Связанные: owners=%d predecessors=%d" % (len(inp["affiliates"]["owners"]), len(inp["affiliates"]["predecessors"])))
    print("Сайт:", inp["data_quality"]["website"])
    print("Сравнимых сделок:", len(inp["comparables"]))
    print("Рыночных запросов:", len(inp["market_research_plan"]["queries"]))
    print("\n", agent_workflow_note())
