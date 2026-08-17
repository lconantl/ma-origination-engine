# -*- coding: utf-8 -*-
"""
market_research.py — рыночный блок тизера (объём рынка, рост, игроки, импортозамещение).

Архитектура (важно): сам веб-поиск выполняет АГЕНТ (инструмент WebSearch/WebFetch),
а этот модуль:
  1) строит ПРИЦЕЛЬНЫЕ поисковые запросы по отрасли/ОКВЭД/продуктам/региону;
  2) задаёт СХЕМУ рыночного брифа (что извлечь из результатов);
  3) тянет M&A-активность отрасли из наших же сравнимых сделок (comparables.py).
Агент: берёт queries -> WebSearch -> заполняет market_brief по схеме.

Так модуль остаётся провайдер-агностичным и не требует платного search-API.
"""
from __future__ import annotations
import re

try:
    import comparables as _cmp
except Exception:
    _cmp = None

# Человеческие названия отраслей для запросов
SECTOR_RU = {
    "it": "IT / программное обеспечение", "food_service": "общепит / рестораны",
    "retail": "розничная торговля", "wholesale": "оптовая торговля / дистрибуция",
    "manufacturing": "производство", "agro": "сельское хозяйство / АПК",
    "construction": "строительство / девелопмент", "real_estate": "коммерческая недвижимость",
    "healthcare": "частная медицина / клиники", "pharma": "фармацевтика / аптеки",
    "transport_logistics": "транспорт и логистика", "mining": "добыча / недропользование",
    "education": "образование / EdTech", "financial": "финансовые услуги",
}


def build_queries(sector: str, okved_name: str = "", products: str = "",
                  region: str = "Россия", year: str = "2025 2026") -> list[str]:
    """Прицельные русскоязычные запросы для рыночного блока тизера."""
    topic = okved_name or SECTOR_RU.get(sector, "отрасль")
    prod = (products or topic).strip()
    q = [
        f"объём рынка {prod} {region} {year} млрд руб прогноз",
        f"рынок {topic} {region} динамика рост темпы {year}",
        f"ключевые игроки конкуренты рынка {prod} {region}",
        f"{topic} импортозамещение уход иностранных игроков {region} {year}",
        f"M&A сделки слияния поглощения {topic} {region} {year}",
    ]
    # для активо-ёмких — спрос-драйверы
    if sector in ("manufacturing", "wholesale", "transport_logistics"):
        q.append(f"спрос {prod} {region} {year} объём продаж динамика отрасли")
    return q


def market_brief_schema() -> dict:
    """Поля рыночного брифа, которые агент заполняет из результатов поиска."""
    return {
        "market_size_rub": None,        # объём рынка, ₽ (с годом)
        "market_size_year": None,
        "cagr_pct": None,               # темп роста рынка, %/год
        "forecast": None,               # прогноз (1-2 предложения + цифра к году)
        "key_players": [],              # 3-7 конкурентов
        "company_position": None,       # позиция/доля оцениваемой компании, если известна
        "tailwinds": [],                # драйверы: импортозамещение, господдержка, спрос
        "risks": [],                    # риски: ставка ЦБ, спад спроса, регуляторика
        "sources": [],                  # ссылки на источники (для доверия в тизере)
        "ma_activity": None,            # из sector_ma_activity()
    }


def sector_ma_activity(sector: str, n: int = 5) -> dict:
    """M&A-активность отрасли из наших сравнимых сделок (для доверия в тизере)."""
    if _cmp is None:
        return {"available": False}
    try:
        deals = _cmp.find_comparables(sector, n=n)
        stats = _cmp.sector_multiple_stats(sector)
        return {
            "available": True,
            "recent_deals": [
                {"date": d["date"], "value": d["deal_value_raw"],
                 "ev_sales": d["ev_sales"], "title": d["title"][:90]}
                for d in deals
            ],
            "median_ev_sales": stats.get("median_ev_sales"),
            "observations": stats.get("n"),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def research_plan(sector: str, okved_name: str = "", products: str = "",
                  region: str = "Россия") -> dict:
    """Готовый план рыночного ресёрча для агента: запросы + схема + M&A-активность."""
    return {
        "sector": sector,
        "sector_ru": SECTOR_RU.get(sector, sector),
        "queries": build_queries(sector, okved_name, products, region),
        "brief_schema": market_brief_schema(),
        "ma_activity": sector_ma_activity(sector),
        "instructions_for_agent": (
            "Выполни WebSearch по каждому query, извлеки цифры (объём рынка, CAGR, "
            "прогноз), 3-7 конкурентов, драйверы (импортозамещение/господдержка/спрос) "
            "и риски. Заполни brief_schema. ОБЯЗАТЕЛЬНО сохрани ссылки в sources — "
            "они идут в тизер как подтверждение. Цифры бери свежие (2025-2026), "
            "приоритет источникам: отраслевые отчёты, РБК, Коммерсант, Ведомости, "
            "Strategy Partners, профильные ассоциации."
        ),
    }


if __name__ == "__main__":
    import sys, json
    sector = sys.argv[1] if len(sys.argv) > 1 else "manufacturing"
    okved = sys.argv[2] if len(sys.argv) > 2 else "автокомпоненты для коммерческого транспорта"
    plan = research_plan(sector, okved_name=okved, products="запчасти для грузовиков")
    print("=== ЗАПРОСЫ для агента ===")
    for q in plan["queries"]:
        print("  •", q)
    print("\n=== M&A-активность отрасли (из comparables) ===")
    print(json.dumps(plan["ma_activity"], ensure_ascii=False, indent=2)[:900])
