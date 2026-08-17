# -*- coding: utf-8 -*-
"""
comparables.py — подбор сравнимых M&A-сделок для секции «Оценка и аналоги» тизера.

Источник: Messages_M&A Новости.csv (поток постов аналитиков). Модуль:
  1) парсит сделки с раскрытой суммой/оценкой;
  2) тегирует отрасль (по ключевым словам);
  3) извлекает выручку из поста (если есть) -> вычисляет EV/Sales;
  4) find_comparables(sector, n) -> топ свежих аналогов с мультипликаторами.

Использование:
    import comparables as cmp
    deals = cmp.load_deals()                  # все распарсенные сделки
    analogs = cmp.find_comparables("food_service", n=5)
"""
from __future__ import annotations
import csv, re, json
from pathlib import Path
from datetime import datetime

csv.field_size_limit(10**7)
CSV_PATH = Path(__file__).with_name("Messages_M&A Новости.csv")
CACHE = Path(__file__).with_name(".comparables_cache.json")

# --- отрасль по ключевым словам (скоринг: больше совпадений -> отрасль) ---
# Порядок = приоритет при равенстве очков (специфичные раньше, IT последним).
SECTOR_KW = {
    "food_service":  r"ресторан|кафе|пиццери|общепит|фастфуд|кофейн|столов|доставк[аи] еды|фабрик[аи]-кухн|бургер",
    "agro":          r"агрохолдинг|птицефабрик|свином|молочн|зернов|урожай|сельхоз|КРС|куриных яиц|масложир|хлебозавод|тепличн",
    "real_estate":   r"гостиниц|отел[ья]|апартамент|бизнес-центр|\bБЦ\b|логопарк|\bТРЦ\b|\bТЦ\b|арендн|кв\.?\s*м|квадратн|номерн[оы]",
    "healthcare":    r"клиник|медцентр|медицинск|лаборатори|здравоохран|стоматолог|диагностик",
    "pharma":        r"фармацевт|лекарств|препарат|аптечн|аптек",
    "mining":        r"месторожд|газодобыч|нефтедобыч|золоторуд|углед|рудник|карьер|запас[ыов]\s|недропользов",
    "construction":  r"застройщик|девелопер|строительн|жилой комплекс|\bЖК\b",
    "transport_logistics": r"логистич|грузопереш|перевозчик|транспортн компани|вагоноремонт|фулфилмент",
    "retail":        r"ритейл|розничн сет|сеть магазин|супермаркет|дискаунтер|магазин[ыа] у дома|ювелирн сет",
    "wholesale":     r"дистрибьютор|оптов|дистрибуц",
    "education":     r"образовательн|edtech|онлайн-школ|онлайн-курс|обучени",
    "financial":     r"\bбанк\b|\bМФО\b|лизингов|страхов|платёжн|финтех",
    "manufacturing": r"завод|производител|фабрик|комбинат|выпуск продукц|изготовл|металлотрейд",
    "it":            r"платформ|SaaS|маркетплейс|нейросет|\bAI\b|облачн|разработчик ПО|разработк[аи] (ПО|программн)|айти-|IT-компани|стартап|онлайн-сервис|кинотеатр|видеонаблюден|реестр российского ПО",
}
_SECTOR_ORDER = list(SECTOR_KW.keys())

VAL_PATTERNS = [
    r'EV\s*[=:—-]\s*([\d\s.,]+(?:[–-][\d\s.,]+)?\s*(?:млрд|млн))',
    r'(?:оцен\w*|сумм\w* сделк\w*|стоимост\w*)[^.]{0,60}?([\d\s.,]+(?:\s*[–-]\s*[\d\s.,]+)?\s*(?:млрд|млн)\s*руб)',
    r'([\d\s.,]+\s*[–-]\s*[\d\s.,]+\s*(?:млрд|млн)\s*руб)',
    r'(\d[\d\s.,]*\s*(?:млрд|млн)\s*руб)',
]
REV_PAT = r'выручк\w*[^.]{0,40}?(\d[\d\s.,]*\s*(?:млрд|млн))'


def _to_mln(s: str):
    """'1,5 млрд' / '700 млн' / '4,5–5 млрд' -> среднее в млн руб."""
    if not s:
        return None
    s = s.replace("\xa0", " ")
    mult = 1000 if "млрд" in s else 1
    nums = re.findall(r'\d[\d\s]*[.,]?\d*', s)
    vals = []
    for n in nums:
        n = n.replace(" ", "").replace(",", ".")
        try:
            vals.append(float(n) * mult)
        except ValueError:
            pass
    return sum(vals) / len(vals) if vals else None


def _tag_sector(text: str):
    """Скоринг: считаем совпадения по каждой отрасли, берём максимум.
    При равенстве — по приоритету (специфичные раньше IT)."""
    best, best_score = None, 0
    for sec in _SECTOR_ORDER:
        score = len(re.findall(SECTOR_KW[sec], text, re.I))
        if score > best_score:
            best, best_score = sec, score
    return best


def _extract_val(text: str):
    for pat in VAL_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return _to_mln(m.group(1)), m.group(1).strip()
    return None, None


def load_deals(date_from="2025-05-01", date_to="2026-06-30", use_cache=True):
    if use_cache and CACHE.exists():
        try:
            return json.loads(CACHE.read_text("utf-8"))
        except Exception:
            pass
    if not CSV_PATH.exists():
        return []
    lo, hi = datetime.fromisoformat(date_from), datetime.fromisoformat(date_to)
    # реальная сделка: конкретный глагол приобретения/продажи (не «инвест»/«сделк» вообще)
    deal_kw = re.compile(r'(приобрел|приобрёл|приобрет[аё]|купил|выкупил|купить|продал|продаёт|закрыл[аи]? сделку|вошёл в капитал|вошл[аи] в капитал|сменил[аи]? владельц|получил[аи]? \d+\s*%|консолидир)', re.I)
    excl = re.compile(r'(ПАО|публичн|\bIPO\b|Мосбирж|X5|Сбербанк|\bВТБ\b|Газпром|международн|глобальн)', re.I)
    # отсев аналитики/дайджестов (не сделки)
    digest = re.compile(r'(основные ошибки|новые ниши|тренд[ыов]|статистик|обзор рынка|дайджест|подборк|как (выбрать|оценить)|почему|рейтинг|итоги (недели|месяца|года))', re.I)
    deals = []
    for r in csv.DictReader(open(CSV_PATH, encoding='utf-8-sig'), delimiter=';'):
        m = r.get('message') or ''
        if len(m) < 120 or not deal_kw.search(m) or digest.search(m[:80]):
            continue
        try:
            d = datetime.fromisoformat((r.get('date') or '').replace('+00:00', ''))
        except Exception:
            continue
        if not (lo <= d <= hi) or excl.search(m):
            continue
        val, val_raw = _extract_val(m)
        if not val:
            continue
        sec = _tag_sector(m)
        revm = _to_mln(re.search(REV_PAT, m, re.I).group(1)) if re.search(REV_PAT, m, re.I) else None
        ev_sales = round(val / revm, 2) if (val and revm) else None
        # санити: реалистичный диапазон EV/Sales для M&A; иначе парсинг выручки кривой
        if ev_sales is not None and not (0.05 <= ev_sales <= 20):
            ev_sales = None
        title = re.sub(r'\s+', ' ', re.sub(r'\*+', '', m[:120])).strip()
        deals.append({
            "date": d.date().isoformat(), "title": title, "sector": sec,
            "deal_value_mln": round(val, 1), "deal_value_raw": val_raw,
            "revenue_mln": round(revm, 1) if revm else None, "ev_sales": ev_sales,
            "snippet": re.sub(r'\s+', ' ', m[:400]).strip(),
        })
    deals.sort(key=lambda x: x["date"], reverse=True)
    try:
        CACHE.write_text(json.dumps(deals, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return deals


def find_comparables(sector: str, n: int = 5, with_multiple_only=False):
    """Топ-N свежих сделок в отрасли. with_multiple_only=True -> только где есть EV/Sales."""
    deals = load_deals()
    pool = [d for d in deals if d["sector"] == sector]
    if with_multiple_only:
        pool = [d for d in pool if d["ev_sales"]]
    return pool[:n]


def sector_multiple_stats(sector: str):
    """Сводка по отрасли: медиана EV/Sales и число наблюдений (для калибровки/обоснования)."""
    deals = [d for d in load_deals() if d["sector"] == sector and d["ev_sales"]]
    if not deals:
        return {"sector": sector, "n": 0, "median_ev_sales": None}
    vals = sorted(d["ev_sales"] for d in deals)
    med = vals[len(vals) // 2]
    return {"sector": sector, "n": len(deals), "median_ev_sales": med,
            "range_ev_sales": [vals[0], vals[-1]]}


if __name__ == "__main__":
    import sys
    deals = load_deals(use_cache=False)
    print(f"Распарсено сделок с оценкой (05.2025–06.2026): {len(deals)}")
    by_sec = {}
    for d in deals:
        by_sec[d["sector"]] = by_sec.get(d["sector"], 0) + 1
    print("По отраслям:", dict(sorted(by_sec.items(), key=lambda x: -x[1])))
    sec = sys.argv[1] if len(sys.argv) > 1 else "food_service"
    print(f"\n=== Аналоги для '{sec}' ===")
    for d in find_comparables(sec, n=6):
        print(f"  [{d['date']}] {d['deal_value_raw']:18} EV/Sales={d['ev_sales']}  {d['title'][:70]}")
    print("\nСтатистика мультипликатора:", sector_multiple_stats(sec))
