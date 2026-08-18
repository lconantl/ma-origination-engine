# -*- coding: utf-8 -*-
"""
generate_teaser.py — автоматический оркестратор: ИНН (+опц. сайт) -> PDF-тизер.

Поток:
  1. financials (ГИР БО, free) — если нет данных -> None («не найдено»).
  2. collect_inputs (company_info/risks/affiliates/scale/macro/сайт/фото) с кэшем.
  3. Claude (прокси opus-4-8) по данным + мультипликаторам + методике -> JSON-контент
     тизера (схема render_html) + данные графиков (market, football-field) + оценка.
  4. chart_builder -> PNG (revenue/profit/market/football). Секция аналогов убрана.
  5. фото (hero/company) best-effort. Нет фото -> чистая navy-обложка.
  6. render_html -> PDF.

CLI:  python generate_teaser.py <ИНН> [сайт]
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import yaml

import data_fetcher as df
import teaser_agent as ta
import chart_builder as cb
import render_html as rh
import claude_client as cc

try:
    import image_search as imgs
except Exception:
    imgs = None

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "output"

# teaser_agent._sector -> ключ в valuation_params.multiples.
# Большинство имён совпадают; только эти отличаются (иначе молча уходило в default).
_SECTOR_ALIAS = {"it": "it_software", "agro": "agriculture", "mining": "default"}


def _mln(x):
    """тыс.руб -> млн.руб (округл. до целого)."""
    return None if x in (None, "") else round(float(x) / 1000.0)


def _params_path() -> Path:
    """Приватная калибровка, если есть; иначе публичный пример."""
    real = ROOT / "valuation_params.yaml"
    return real if real.exists() else ROOT / "valuation_params.example.yaml"


def _multiples(sector: str) -> dict:
    params = yaml.safe_load(_params_path().read_text("utf-8"))
    m = params.get("multiples", {})
    # прямое совпадение имени сектора → алиас → default (без тихой потери отраслевых чисел)
    key = sector if sector in m else _SECTOR_ALIAS.get(sector, "default")
    block = m.get(key) or m.get("default")
    adj = params.get("adjustments", {})
    return {
        "sector_key": key,
        "ev_ebitda": block.get("ev_ebitda"),
        "ev_sales": block.get("ev_sales"),
        "notes": block.get("notes", ""),
        "dlom": adj.get("dlom", {}).get("default"),
        "control_premium": adj.get("control", {}).get("control_premium"),
        "size_thresholds_rub": params.get("thresholds", {}).get("size_revenue_rub"),
        "ebit_to_ebitda": params.get("ebitda_proxy", {}).get("fallback_ebit_to_ebitda"),
    }


# ---------------- сбор компактного payload для Claude ----------------
def _financials_payload(fin: dict) -> dict:
    years = fin.get("years", {})
    out = {}
    for y in sorted(years):
        r = years[y]
        out[y] = {k: _mln(r.get(k)) for k in
                  ("revenue", "gross_profit", "ebit", "net_profit", "assets",
                   "equity", "debt", "cash", "net_debt")}
    return out


def _company_payload(ci: dict) -> dict:
    return {
        "name_full": ci.get("name_full"), "name_short": ci.get("name_short"),
        "status": ci.get("status_text") or ci.get("status"),
        "registration_date": ci.get("registration_date"),
        "okved_code": ci.get("okved_code"), "okved_name": ci.get("okved_name"),
        "address": ci.get("address"), "region": ci.get("region"),
        "manager": ci.get("manager"), "founders": ci.get("founders"),
        "employees": ci.get("employees"), "authorized_capital": ci.get("authorized_capital"),
        "is_msp": ci.get("is_msp"), "msp_category": ci.get("msp_category"),
        "is_ip": ci.get("is_ip"),
    }


def _risk_payload(risk: dict) -> dict:
    if not isinstance(risk, dict):
        return {}
    # ключи синхронизированы с реальным выводом data_fetcher.get_risk_flags
    keys = ("is_bankrupt", "is_liquidating", "tax_arrears_flag", "tax_debt",
            "account_blocks", "legal_cases", "enforcements", "negative_registries",
            "licenses", "flags", "risk_score")
    return {k: risk.get(k) for k in keys if k in risk}


def _website_excerpt(site) -> str:
    if not site or not isinstance(site, dict) or site.get("error"):
        return ""
    parts = []
    for k in ("title", "description", "about", "text"):
        v = site.get(k)
        if v:
            parts.append(str(v))
    txt = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return txt[:2500]


SYSTEM_PROMPT = """Ты — старший аналитик M&A-бутика (уровень McKinsey / Strategy Partners).
Готовишь инвестиционный тизер по российской компании для холодной продажи бизнеса
владельцу. Твоя оценка должна быть РЕАЛИСТИЧНОЙ и заведомо КОНСЕРВАТИВНОЙ (по публичной
отчётности), но профессиональной.

ЖЁСТКИЕ ПРАВИЛА:
1. Все финансовые цифры бери ТОЛЬКО из предоставленных данных (они в млн руб). Не выдумывай
   выручку/прибыль/активы. Если поля нет — не пиши его.

2. ОЦЕНКА EV (стоимость бизнеса, 100%) — ПРОЙДИ ДЕРЕВО РЕШЕНИЙ сверху вниз, первое
   сработавшее условие задаёт метод(ы):
   N1. Банкротство/ликвидация ИЛИ капитал<0 ≥2 лет и убытки ≥3 лет →
       ЛИКВИДАЦИОННАЯ / Чистые активы с дисконтом (метод «Ликвидационная стоимость»).
   N2. ОКВЭД 68 (недвижимость/аренда) → капитализация дохода (NOI/cap rate) + NAV по рынку.
       ОКВЭД 64/65/66 (банк/МФО/лизинг/финансы) → по капиталу (P/B) + P/E.
       Холдинг (активы≫прибыль, выручки почти нет) → Чистые активы (NAV, рыночная переоценка).
   N3. Операционный бизнес:
       • стабильно прибыльный (EBITDA>0 ≥2-3 лет) → мультипликаторы + (для среднего/крупного) DCF;
       • убыточный, но быстрый рост выручки (>30%/год) → по выручке (EV/Sales) + DCF с фазой роста;
       • прибыль волатильна/разовые всплески → НОРМАЛИЗУЙ (среднее 3 лет, убери разовое),
         вес мультипликатора выше DCF;
       • убыточный без роста → Чистые активы/ликвидация как «пол» стоимости.
   N4. Размер: малый/микро (выручка<800 млн ₽) → ОСНОВНОЙ мультипликатор (EV/EBITDA или P/E),
       кросс-чек SDE/NAV. Средний (0.8-2 млрд) → DCF + EV/EBITDA. Крупный (>2 млрд) → DCF + аналоги.
   N5. EBITDA нормализуй; при отсутствии амортизации EBITDA≈EBIT×1.15. Применяй ДАННЫЙ отраслевой
       мультипликатор [low/mid/high]. Мост EV→Equity: вычти чистый долг (или прибавь чистый кэш).
   N6. Корректировки: скидка за размер (малый бизнес), DLOM, контроль (продаётся контроль →
       без миноритарного дисконта), концентрация на собственнике. Диапазон, не точка.
   N7. ОБЯЗАТЕЛЬНО учти переданные данные в оценке и тексте:
       • risk_flags: is_bankrupt/is_liquidating → ветвь N1 (ликвидационная). risk_score высокий,
         account_blocks (блокировки счетов), tax_arrears_flag/tax_debt, заметные legal_cases в роли
         ОТВЕТЧИКА, enforcements (ФССП) → дополнительный дисконт к оценке и пункт в «рисках».
       • scale_signals: если gray_flag=true / official_to_implied_ratio указывает на серую выручку —
         это повышает истинный масштаб, но публичная оценка остаётся консервативной (отрази в
         upside/тексте, не завышай официальную базу).
       • perimeter_hint (учредители/предшественники): если бизнес размазан по ИП/связанным ООО —
         укажи это в deal_perimeter и оговори, что оценка по одному юрлицу — «пол» группы.
   Будь РЕАЛИСТИЧЕН и КОНСЕРВАТИВЕН для частного бизнеса РФ.
   КАЛИБРОВКА МУЛЬТИПЛИКАТОРА (важно, не завышай):
   • ВЕРХНЮЮ границу отраслевого диапазона применяй ТОЛЬКО к стабильно растущему бизнесу без
     риск-флагов и без завязки на собственника. При волатильной прибыли, обвале/просадке
     последнего года, малом размере или высокой зависимости от собственника — используй
     НИЖНЮЮ–СРЕДНЮЮ часть диапазона (low..mid).
   • В нормализацию EBITDA ВКЛЮЧАЙ последний доступный год (взвешенно), если он отражает
     текущую реальность; не отбрасывай слабый год целиком как «разовый» без явных оснований.
     Не якори оценку на единственном пиковом годе.
   • NAV (чистые активы) — это «пол» оценки; итоговая база ev_base ближе к нижней-средней части,
     а не к максимуму EV/EBITDA.

3. football-field: верни 2-3 метода, КОТОРЫЕ РЕАЛЬНО ВЫБРАЛО дерево (например «EV/EBITDA (норм.)»
   и «Чистые активы (NAV)»; для недвижимости — «Капитализация дохода»; для растущих — «EV/Sales»),
   каждый с low/high, и итоговую базу ev_base внутри пересечения диапазонов.
   ВАЖНО ПРО ДЛИНУ ТЕКСТА (иначе ломается вёрстка слайда):
   • "method" — КОРОТКО, ≤ 90 символов (название метода, без расписывания дерева).
   • "justification" — ровно 1-2 предложения, ≤ 260 символов, только суть с ключевыми цифрами.
   Подробный разбор дерева НЕ выводи в эти поля — он не нужен в презентации.
   Для проверки маржинальности используй industry_benchmark (ebitda_margin/net_margin отрасли).

4. Рынок: дай оценку объёма рынка/сегмента РФ и динамику (4 года, млрд руб) на основе своих
   знаний отрасли — пометь как отраслевую оценку. Тексты — деловые, без воды.
5. action_title КАЖДОГО слайда — это законченный ТЕЗИС (вывод с цифрой), не заголовок темы.
6. Язык — русский, тон — уверенный и фактический. Кавычки «ёлочки».
7. Верни СТРОГО валидный JSON по схеме ниже. Без markdown, без комментариев, без текста вне JSON."""


def _user_prompt(bundle: dict, fin_payload: dict, mult: dict) -> str:
    ci = bundle["company_info"]
    data = {
        "inn": bundle["inn"],
        "sector": bundle["sector"],
        "company": _company_payload(ci),
        "financials_mln_rub": fin_payload,
        "risk_flags": _risk_payload(bundle.get("risk_flags")),
        "perimeter_hint": {
            "owners": (bundle.get("affiliates") or {}).get("owners"),
            "predecessors": (bundle.get("affiliates") or {}).get("predecessors"),
        },
        "scale_signals": bundle.get("scale_signals"),
        "macro": bundle.get("macro"),
        "valuation_multiples": mult,
        "industry_benchmark": bundle.get("benchmark"),
        "website_excerpt": _website_excerpt(bundle.get("website")),
    }
    schema = {
        "content": {
            "company_name": "ООО «Название» · ИНН 0000000000",
            "cover": {"positioning": "<позиционирование 1 фразой>",
                      "subtitle": "Инвестиционная возможность", "date": "<месяц год>"},
            "summary": {"action_title": "<тезис>", "one_liner": "<2-3 предложения о компании>",
                        "highlights": ["<6 буллетов-преимуществ>"],
                        "source": "ФНС / ГИР БО; анализ",
                        "key_figures": {"revenue": "<X млн ₽>", "ebitda": "<X–Y млн ₽>",
                                        "clients": "<если известно, иначе опусти>",
                                        "ev_range": "<low–high млн ₽>"}},
            "company": {"action_title": "<тезис>", "founded": "<год/история>",
                        "location": "<город/регион>", "team_size": "<кол-во сотр.>",
                        "brands": ["<бренды>"], "business_model": "<модель>",
                        "exclusives": ["<2-4 уникальных преимущества>"],
                        "deal_perimeter": ["<юрлица/ИП периметра сделки>"]},
            "market": {"action_title": "<тезис с размером рынка>",
                       "tailwinds": ["<3-4 драйвера>"], "risks": ["<1-2 риска>"],
                       "sources": ["<1-3 источника>"]},
            "financials": {"action_title": "<тезис о динамике>",
                           "commentary": "<3-4 предложения: динамика, долг, капитал, EBITDA>"},
            "highlights_slide": {"action_title": "<тезис о точках роста>",
                                 "catalysts": [{"title": "<кратко>", "text": "<1-2 предложения>"}]},
            "valuation": {"action_title": "<тезис с диапазоном оценки>",
                          "method": "<метод>", "justification": "<обоснование с цифрами>",
                          "upside_drivers": ["<4-5 факторов, что увеличит оценку>"]},
            "contacts": {"headline": "Готовы к диалогу", "cta": "<призыв, 2 предложения>",
                         "advisor": "Инициатор: стратегический отраслевой инвестор · [имя] · [телефон] · [e-mail]",
                         "confidentiality": "Документ носит индикативный характер, не является офертой. Конфиденциально."}
        },
        "chart_data": {
            "market": {"years": ["2022", "2023", "2024", "2025"], "values": [0, 0, 0, 0],
                       "unit": "млрд ₽", "title": "<название рынка>",
                       "last_note": "<опц. сноска про последний год>"},
            "valuation_methods": [
                {"name": "EV/EBITDA (норм.)", "low": 0, "high": 0},
                {"name": "Чистые активы (NAV)", "low": 0, "high": 0}],
            "ev_base": 0
        }
    }
    return ("ДАННЫЕ КОМПАНИИ (JSON):\n" + json.dumps(data, ensure_ascii=False, indent=1) +
            "\n\nВЕРНИ JSON СТРОГО ПО ЭТОЙ СХЕМЕ (значения-плейсхолдеры замени реальными, "
            "catalysts — ровно 4, highlights — до 6, valuation_methods — 2-3 метода, ВЫБРАННЫЕ "
            "деревом решений под этот бизнес; названия методов могут отличаться от примера):\n" +
            json.dumps(schema, ensure_ascii=False, indent=1))


# ---------------- валидация/нормализация контента ----------------
_REQUIRED_CONTENT = ("company_name", "cover", "summary", "company", "market",
                     "financials", "highlights_slide", "valuation", "contacts")


def _validate_and_fill(parsed: dict) -> tuple[dict, dict]:
    if not isinstance(parsed, dict) or "content" not in parsed or "chart_data" not in parsed:
        raise cc.ClaudeError("ответ Claude без content/chart_data")
    c = parsed["content"]
    for k in _REQUIRED_CONTENT:
        if k not in c:
            raise cc.ClaudeError(f"в content нет секции: {k}")
    # дефолты/инъекции
    c["cover"].setdefault("subtitle", "Инвестиционная возможность")
    c["valuation"]["disclaimer"] = ta.DISCLAIMER
    c["valuation"].setdefault("upside_drivers", ta.UPSIDE_CHECKLIST)
    ct = c["contacts"]
    ct.setdefault("headline", "Готовы к диалогу")
    ct.setdefault("confidentiality",
                  "Документ носит индикативный характер, не является офертой. Конфиденциально.")
    cd = parsed["chart_data"]
    # приведение типов: Claude иногда отдаёт числа строками/null → графики падали молча
    def _num(x):
        try:
            return float(str(x).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return None
    methods = []
    for mt in (cd.get("valuation_methods") or []):
        lo, hi = _num(mt.get("low")), _num(mt.get("high"))
        if lo is not None and hi is not None and mt.get("name"):
            methods.append({"name": str(mt["name"]), "low": lo, "high": hi})
    cd["valuation_methods"] = methods
    cd["ev_base"] = _num(cd.get("ev_base"))
    mk = cd.get("market") or {}
    if mk.get("values"):
        vals = [_num(v) for v in mk["values"]]
        yrs = mk.get("years") or []
        pairs = [(y, v) for y, v in zip(yrs, vals) if v is not None]
        if len(pairs) >= 2:
            mk["years"] = [p[0] for p in pairs]
            mk["values"] = [p[1] for p in pairs]
        else:
            mk = {}
    cd["market"] = mk or None
    if not methods or cd["ev_base"] in (None, 0):
        raise cc.ClaudeError("chart_data без валидных valuation_methods/ev_base")
    return c, cd


# ---------------- фото: скачать 12-15 → Claude-vision отбирает лучшие ----------------
def _thumb_b64(path: str, max_px: int = 300) -> str | None:
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((max_px, max_px))
        tmp = str(Path(path).with_suffix(".thumb.jpg"))
        im.save(tmp, quality=70)
        return tmp
    except Exception:
        return None


def _vision_pick(saved: list[dict], company_desc: str):
    """Claude-vision выбирает hero (обложка-баннер) и второе фото. -> (hero_path, company_path)."""
    items = [s for s in saved if s.get("path") and Path(s["path"]).exists()][:14]
    if not items:
        return None, None
    blocks = [{"type": "text", "text":
               f"Компания: {company_desc}.\nНиже {len(items)} изображений-кандидатов "
               "(пронумерованы 0..N) для инвестиционного тизера уровня McKinsey.\n"
               "Выбери:\n• hero — горизонтальное профессиональное фото для ОБЛОЖКИ "
               "(релевантно бизнесу/отрасли, БЕЗ текста/логотипов/водяных знаков, "
               "хорошо смотрится под тёмным сине-навигационным наложением);\n"
               "• company — второе, иное по содержанию фото (производство/продукт/офис), либо null.\n"
               "Отдавай предпочтение реалистичным фото, а не клипарту/схемам/коллажам.\n"
               'Верни СТРОГО JSON: {"hero": <индекс>, "company": <индекс|null>}.'}]
    for i, s in enumerate(items):
        th = _thumb_b64(s["path"])
        if not th:
            continue
        blocks.append({"type": "text", "text": f"[{i}] {s.get('title','')[:60]}"})
        blocks.append(cc.image_block(th))
    try:
        txt = cc.complete_blocks("Ты арт-директор инвестиционных презентаций.", blocks,
                                 max_tokens=1000)
        sel = cc.extract_json(txt)
    except Exception:
        return items[0]["path"], (items[1]["path"] if len(items) > 1 else None)
    hi = sel.get("hero"); ci_ = sel.get("company")
    hero = items[hi]["path"] if isinstance(hi, int) and 0 <= hi < len(items) else items[0]["path"]
    comp = items[ci_]["path"] if isinstance(ci_, int) and 0 <= ci_ < len(items) and ci_ != hi else None
    return hero, comp


def _pick_images(bundle: dict, workdir: Path) -> dict:
    images = {"hero": None, "logo": None, "company": None}
    if imgs is None:
        return images
    cands = list((bundle.get("photos") or {}).get("candidates") or [])
    site_imgs = (bundle.get("website") or {}).get("images") or []
    pool = (site_imgs[:5] + cands)[:15]
    if not pool:
        return images
    try:
        results = pool if isinstance(pool[0], dict) else [{"url": u, "title": ""} for u in pool]
        saved = imgs.download_images(results, outdir=str(workdir / "img"), limit=14)
        if not saved:
            return images
        ci = bundle.get("company_info") or {}
        desc = f"{ci.get('name_short','')} — {ci.get('okved_name','')}".strip(" —")
        hero, comp = _vision_pick(saved, desc)
        images["hero"] = hero
        images["company"] = comp
    except Exception:
        pass
    return images


# ---------------- основной вход ----------------
def generate_teaser(inn: str, url: str = "", on_progress=None, with_content: bool = False):
    """ИНН(+сайт) -> путь к PDF (или None). with_content=True -> (pdf, content_dict)."""
    inn = str(inn).strip()
    log = on_progress or (lambda m: None)

    # 1) финансы — гейт «найдено/не найдено»
    fin = df.get_financials(inn)
    if not fin.get("years") or not (fin.get("data_quality") or 0) > 0:
        log("нет финансовых данных")
        return (None, None) if with_content else None

    # 2) бандл данных (с кэшем; OFData экономно за счёт .cache)
    log("собираю данные из реестров…")
    try:
        bundle = ta.collect_inputs(inn, url or "")
    except Exception as e:
        # деградация: хотя бы финансы + минимум company_info
        log(f"частичные данные ({e})")
        bundle = {"inn": inn, "sector": "default",
                  "company_info": df.get_company_info(inn) if hasattr(df, "get_company_info") else {},
                  "financials": fin, "risk_flags": {}, "affiliates": {},
                  "scale_signals": {}, "macro": df.get_macro(), "website": None,
                  "photos": {"candidates": []}}
    bundle["financials"] = fin

    # 2b) СПАРК (премиум-источник) — если настроен в .env, обогащает реквизиты/риски/
    # аффилированность более авторитетными данными. Финансы оставляем из ГИР БО (надёжно).
    try:
        import spark_client
        sp = spark_client.enrich_bundle(inn)
        if sp:
            log("обогащаю данными СПАРК…")
            for k in ("company_info", "risk_flags", "affiliates"):
                if sp.get(k):
                    bundle[k] = {**(bundle.get(k) or {}), **sp[k]}
            bundle["data_sources_premium"] = ["spark"]
    except Exception as e:
        log(f"СПАРК недоступен ({e}) — работаю на бесплатных источниках")

    mult = _multiples(bundle.get("sector", "default"))

    # 3) Claude -> контент + данные графиков
    log("Claude готовит оценку и тексты…")
    # max_tokens высокий не просто так: у reasoning-моделей через Polza скрытые
    # "размышления" считаются в тот же бюджет, что и видимый ответ (usage.completion_
    # tokens_details.reasoning_tokens) — на сложной оценке съедали 6000+ из 8000,
    # JSON тизера обрывался (finish_reason="length") и не парсился.
    parsed = cc.complete_json(SYSTEM_PROMPT,
                              _user_prompt(bundle, _financials_payload(fin), mult),
                              max_tokens=16000, temperature=0.2, retries=1)
    content, cd = _validate_and_fill(parsed)

    # 4) графики
    log("строю графики…")
    OUT_DIR.mkdir(exist_ok=True)
    workdir = OUT_DIR / inn
    workdir.mkdir(parents=True, exist_ok=True)
    valuation = {"methods": cd["valuation_methods"], "base": cd["ev_base"], "unit": "млн ₽"}
    built = cb.build_charts({"financials": fin, "market": cd.get("market"),
                             "valuation": valuation}, outdir=str(workdir / "charts"))
    charts = {b["type"]: b["path"] for b in built}

    # 5) фото
    log("подбираю изображения…")
    images = _pick_images(bundle, workdir)

    # 6) рендер
    log("рендерю PDF…")
    safe = re.sub(r"[^\w\-]+", "_", (content.get("company_name") or inn))[:40]
    out_pdf = str(workdir / f"{safe}_тизер.pdf")
    rh.render(content, charts, images)
    log("готово")
    return (out_pdf, content) if with_content else out_pdf


if __name__ == "__main__":
    inn = sys.argv[1] if len(sys.argv) > 1 else "7743195810"
    url = sys.argv[2] if len(sys.argv) > 2 else ""
    res = generate_teaser(inn, url, on_progress=lambda m: print("·", m))
    print("PDF:", res if res else "НЕ НАЙДЕНО")
