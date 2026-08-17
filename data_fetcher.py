# -*- coding: utf-8 -*-
"""
data_fetcher.py — агрегатор данных по российским компаниям для M&A-оценки.

Публичный интерфейс (фиксированный):
    get_company_info(inn: str) -> dict
    get_financials(inn: str)  -> dict
    get_risk_flags(inn: str)  -> dict
Дополнительно:
    get_macro()               -> dict   # ключевая ставка, ОФЗ (для DCF)
    fetch_all(inn: str)       -> dict   # всё сразу + macro

Архитектура
-----------
* Источники описаны в config.yaml. Смена/добавление/отключение источника и
  приоритетов — правкой конфига, без правки этого файла.
* Для каждого источника есть "парсер" (см. PARSERS) — он знает, как из сырого
  ответа собрать канонические поля. Парсеры для всех штатных источников уже есть.
* Маршрутизация (routing) задаёт порядок источников по каждому блоку данных.
  Fetcher идёт по списку: заполняет поля из первого ответившего источника,
  затем ДОЗАПОЛНЯЕТ пропуски из следующих (merge), оставшиеся сбои — fallback.
* Любая ошибка источника не роняет процесс: поля остаются None,
  считается data_quality (0.0–1.0) по доле заполненных ключевых полей.

Зависимости: requests, PyYAML. (lxml/bs4 — опционально, для scrape Checko.)
"""

from __future__ import annotations

import json
import os
import re
import time
import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit("Нужен PyYAML: pip install pyyaml") from e

log = logging.getLogger("data_fetcher")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _load_dotenv() -> None:
    """Подгружает ключи из файла .env рядом со скриптом (KEY=VALUE), не перезаписывая
    уже заданные переменные окружения. python-dotenv не требуется."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()

CONFIG_PATH = os.environ.get("DF_CONFIG", str(Path(__file__).with_name("config.yaml")))


# ============================================================================
#  Канонические схемы (ключевые поля -> вес для data_quality)
# ============================================================================
COMPANY_FIELDS = {
    "ogrn": 1, "name_short": 1, "status": 2, "registration_date": 1,
    "okved_code": 2, "okopf": 1, "address": 1, "manager": 2,
    "employees": 1, "is_msp": 1,
}
FIN_FIELDS = {  # на один год
    "revenue": 3, "net_profit": 3, "assets": 2, "equity": 2,
    "debt": 2, "ebitda": 1,
}
RISK_FIELDS = {
    "status": 2, "is_bankrupt": 2, "legal_cases": 2, "enforcements": 2,
    "tax_debt": 1, "gov_contracts": 1, "account_blocks": 1,
}


def _empty_company(inn: str) -> dict:
    return {
        "inn": inn, "ogrn": None, "kpp": None, "name_full": None, "name_short": None,
        "status": None, "status_text": None, "registration_date": None,
        "okved_code": None, "okved_name": None, "okopf": None,
        "address": None, "region": None, "manager": None, "founders": None,
        "employees": None, "authorized_capital": None,
        "is_msp": None, "msp_category": None, "is_ip": False,
        "sources_used": [], "data_quality": 0.0,
    }


def _empty_financials(inn: str) -> dict:
    return {
        "inn": inn, "currency": "RUB", "unit": "thousand",
        "years": {}, "latest_year": None,
        "sources_used": [], "data_quality": 0.0,
    }


def _empty_risk(inn: str) -> dict:
    return {
        "inn": inn, "status": None,
        "is_bankrupt": None, "is_liquidating": None,
        "tax_debt": None, "tax_arrears_flag": None,
        "legal_cases": None, "enforcements": None, "account_blocks": None,
        "gov_contracts": None, "licenses": None, "negative_registries": None,
        "affiliated": None,
        "flags": [], "risk_score": None,
        "sources_used": [], "data_quality": 0.0,
    }


def _empty_year() -> dict:
    return {k: None for k in (
        "revenue", "gross_profit", "ebit", "ebitda", "net_profit",
        "assets", "equity", "debt", "total_liabilities", "cash",
        "net_debt", "depreciation", "interest_paid",
    )}


# ============================================================================
#  Утилиты слияния / качества
# ============================================================================
def _merge_fill(dst: dict, src: dict) -> None:
    """Заполняет в dst только пустые (None / пустой список) поля значениями из src."""
    for k, v in src.items():
        if v in (None, [], {}, ""):
            continue
        if dst.get(k) in (None, [], {}, ""):
            dst[k] = v


def _quality(d: dict, fields: Dict[str, int]) -> float:
    total = sum(fields.values())
    got = sum(w for k, w in fields.items() if d.get(k) not in (None, [], {}, ""))
    return round(got / total, 3) if total else 0.0


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-eE]", "", s)
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _norm_status(code: Optional[str], text: Optional[str]) -> Optional[str]:
    """Приводит произвольный статус к: ACTIVE / LIQUIDATING / BANKRUPT / LIQUIDATED / REORG."""
    blob = " ".join(str(x).lower() for x in (code, text) if x)
    if not blob:
        return None
    if "банкрот" in blob or "bankrupt" in blob or "конкурсн" in blob:
        return "BANKRUPT"
    if "ликвидир" in blob or "liquidating" in blob or "в стадии ликвидации" in blob:
        return "LIQUIDATING"
    if "ликвидир" in blob and "прекращ" in blob:
        return "LIQUIDATED"
    if "прекращ" in blob or "исключен" in blob:
        return "LIQUIDATED"
    if "реорг" in blob or "reorg" in blob:
        return "REORG"
    if "действ" in blob or "active" in blob:
        return "ACTIVE"
    return code or text


# ============================================================================
#  Источник (Source): транспорт + auth + кэш + ретраи
# ============================================================================
class Source:
    def __init__(self, name: str, cfg: dict, settings: dict, secrets_env: Dict[str, str]):
        self.name = name
        self.cfg = cfg
        self.settings = settings
        self.parser_name = cfg.get("parser", name)
        self.base_url = cfg.get("base_url", "").rstrip("/")
        self.scrape_url = cfg.get("scrape_url", "").rstrip("/")
        self.auth = cfg.get("auth", {"type": "none"})
        self.method = cfg.get("method", "GET").upper()
        self.endpoints = cfg.get("endpoints", {})
        self._secrets_env = secrets_env
        self._last_call = 0.0

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.get("user_agent", "Mozilla/5.0")})

        self.cache_dir = Path(settings.get("cache_dir", ".cache"))
        self.cache_ttl = settings.get("cache_ttl_hours", 24) * 3600

    # --- доступность -------------------------------------------------------
    def secret(self) -> Optional[str]:
        sname = self.auth.get("secret")
        if not sname:
            return None
        env_name = self._secrets_env.get(sname)
        return os.environ.get(env_name) if env_name else None

    def available(self) -> bool:
        if not self.cfg.get("enabled", True):
            return False
        # источникам с auth, требующим секрет, нужен заданный секрет
        if self.auth.get("type") in ("query_param", "header") and self.auth.get("secret"):
            # checko умеет работать без ключа (scrape) — особый случай
            if self.secret() is None and self.parser_name != "checko":
                return False
        return True

    # --- кэш ---------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha1(f"{self.name}:{key}".encode()).hexdigest()[:20]
        return self.cache_dir / f"{self.name}_{h}.json"

    def _cache_get(self, key: str):
        p = self._cache_path(key)
        if p.exists() and (time.time() - p.stat().st_mtime) < self.cache_ttl:
            try:
                return json.loads(p.read_text("utf-8"))
            except Exception:
                return None
        return None

    def _cache_put(self, key: str, value) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        except Exception as e:
            log.debug("cache write failed: %s", e)

    # --- запрос ------------------------------------------------------------
    def _rate_limit(self) -> None:
        gap = self.settings.get("rate_limit_sleep", 0.0)
        delta = time.time() - self._last_call
        if delta < gap:
            time.sleep(gap - delta)
        self._last_call = time.time()

    def request(self, endpoint: str, expect: str = "json", **vars_) -> Any:
        """
        Выполняет запрос к именованному endpoint из конфига.
        vars_ подставляются в path/params/body ({inn}, {org_id}, {key}, ...).
        Возвращает dict (json) / str (text/html) или None при сбое.
        """
        ep = self.endpoints.get(endpoint)
        if ep is None:
            return None

        vars_ = dict(vars_)
        if self.auth.get("type") == "query_param" and "key" not in vars_:
            vars_["key"] = self.secret() or ""
        if "token" not in vars_:
            vars_["token"] = self.secret() or ""

        def fill(s: str) -> str:
            for k, v in vars_.items():
                s = s.replace("{" + k + "}", str(v))
            return s

        path = fill(ep.get("path", ""))
        url = self.base_url + path
        params = {k: fill(str(v)) for k, v in ep.get("params", {}).items()}
        # auth типа query_param: добавляем ключ в query, даже если его нет в params
        if self.auth.get("type") == "query_param" and self.secret():
            params.setdefault(self.auth.get("param", "key"), self.secret())
        method = ep.get("method", self.method).upper()

        headers = {}
        if self.auth.get("type") == "header" and self.secret():
            headers[self.auth["header"]] = self.auth.get("template", "{secret}").replace(
                "{secret}", self.secret())

        json_body = None
        data_body = None
        if "body" in ep:
            json_body = {k: fill(str(v)) for k, v in ep["body"].items()}
            method = "POST"
            headers.setdefault("Content-Type", "application/json")
        if "body_form" in ep:
            data_body = {k: fill(str(v)) for k, v in ep["body_form"].items()}
            method = "POST"

        cache_key = f"{endpoint}|{url}|{json.dumps(params, sort_keys=True)}|{json.dumps(json_body or data_body, sort_keys=True)}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        retries = self.settings.get("retries", 1)
        backoff = self.settings.get("retry_backoff", 1.5)
        timeout = self.settings.get("request_timeout", 20)

        for attempt in range(retries + 1):
            try:
                self._rate_limit()
                resp = self.session.request(
                    method, url, params=params or None,
                    json=json_body, data=data_body, headers=headers or None,
                    timeout=timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    log.warning("%s/%s -> HTTP %s", self.name, endpoint, resp.status_code)
                    return None
                if expect == "json":
                    out = resp.json()
                else:
                    out = resp.text
                self._cache_put(cache_key, out)
                return out
            except Exception as e:
                if attempt < retries:
                    time.sleep(backoff ** attempt)
                    continue
                log.warning("%s/%s failed: %s", self.name, endpoint, e)
                return None


# ============================================================================
#  ПАРСЕРЫ. Каждый парсер реализует часть методов:
#     company_info(src, inn) -> partial dict
#     financials(src, inn)   -> partial dict (с ключом "years")
#     risk_flags(src, inn)   -> partial dict
#  Любой метод может отсутствовать или вернуть {} — это нормально.
# ============================================================================
def _dig(d: Any, *keys, default=None):
    """Безопасный доступ d[k1][k2]... с альтернативами на каждом уровне (кортеж)."""
    cur = d
    for k in keys:
        if cur is None:
            return default
        found = False
        for kk in (k if isinstance(k, tuple) else (k,)):
            if isinstance(cur, dict) and kk in cur:
                cur = cur[kk]
                found = True
                break
        if not found:
            return default
    return cur if cur is not None else default


# ---- OFData (основной агрегатор) ------------------------------------------
class OFDataParser:
    """
    OFData v2 API. Структура: {"data": {...}, "meta": {...}}. Откалибровано по
    живому ответу (06.2026, тариф «Бесплатный»: company/legal-cases/enforcements/
    inspections доступны; finances -> "Недоступно для бесплатного тарифа").
    /company содержит встроенные риск-поля: Лиценз, ЕФРСБ, НедобПост, Санкции,
    Налоги, МассРуковод/МассУчред, ДисквЛица — используются в risk_flags.
    """

    @staticmethod
    def _data(raw):
        if isinstance(raw, dict):
            if raw.get("meta", {}).get("status") == "error":
                return None
            return raw.get("data", raw)
        return None

    def _company_raw(self, src: Source, inn: str):
        # /company дёргаем один раз; кэш Source отдаст тот же ответ для risk_flags
        return self._data(src.request("company", inn=inn))

    def company_info(self, src: Source, inn: str) -> dict:
        d = self._company_raw(src, inn)
        if not d:
            return {}
        out = _empty_company(inn)
        out["ogrn"] = d.get("ОГРН")
        out["kpp"] = d.get("КПП")
        out["name_full"] = d.get("НаимПолн")
        out["name_short"] = d.get("НаимСокр")
        out["status"] = _norm_status(_dig(d, "Статус", "Код"), _dig(d, "Статус", "Наим"))
        out["status_text"] = _dig(d, "Статус", "Наим")
        out["registration_date"] = d.get("ДатаРег")
        out["okved_code"] = _dig(d, "ОКВЭД", "Код")
        out["okved_name"] = _dig(d, "ОКВЭД", "Наим")
        out["okopf"] = _dig(d, "ОКОПФ", "Наим")
        out["address"] = _dig(d, "ЮрАдрес", "АдресРФ") or _dig(d, "ЮрАдрес", "НасПункт")
        out["region"] = _dig(d, "Регион", "Наим")
        rukov = d.get("Руковод")
        if isinstance(rukov, list) and rukov:
            rukov = rukov[0]
        if isinstance(rukov, dict):
            out["manager"] = {"name": rukov.get("ФИО"), "post": rukov.get("НаимДолжн")}
        out["founders"] = d.get("Учред")
        out["employees"] = _to_float(d.get("СЧР"))
        out["authorized_capital"] = _to_float(_dig(d, "УстКап", "Сумма"))
        msp = d.get("РМСП")
        if isinstance(msp, dict) and msp:
            out["is_msp"] = True
            out["msp_category"] = msp.get("Кат") or msp.get("Категория")
        return out

    def entrepreneur(self, src: Source, inn: str) -> dict:
        """ИП (ЕГРИП). У ИП нет бухотчётности — только реквизиты/статус/ОКВЭД."""
        d = self._data(src.request("entrepreneur", inn=inn))
        if not d:
            return {}
        out = _empty_company(inn)
        out["is_ip"] = True
        out["ogrn"] = d.get("ОГРНИП") or d.get("ОГРН")
        out["name_full"] = d.get("ФИО") or d.get("НаимПолн")
        out["name_short"] = d.get("ФИО") or d.get("НаимСокр")
        out["status"] = _norm_status(_dig(d, "Статус", "Код"), _dig(d, "Статус", "Наим"))
        out["status_text"] = _dig(d, "Статус", "Наим")
        out["registration_date"] = d.get("ДатаРег")
        out["okved_code"] = _dig(d, "ОКВЭД", "Код")
        out["okved_name"] = _dig(d, "ОКВЭД", "Наим")
        out["okopf"] = "Индивидуальный предприниматель"
        out["region"] = _dig(d, "Регион", "Наим")
        msp = d.get("РМСП")
        if isinstance(msp, dict) and msp:
            out["is_msp"] = True
            out["msp_category"] = msp.get("Кат") or msp.get("Категория")
        return out

    def affiliates(self, src: Source, inn: str) -> dict:
        """Связанные лица: учредители-ФЛ, руководитель, предшественники + их ИНН.
        Поиск ДРУГИХ компаний учредителей — через search_founder (опционально,
        может быть недоступен на free-тарифе)."""
        d = self._company_raw(src, inn)
        if not d:
            return {}
        owners = []
        uchr = d.get("Учред") or {}
        for fl in (uchr.get("ФЛ") or []):
            if fl.get("ИНН") or fl.get("ФИО"):
                owners.append({"name": fl.get("ФИО"), "inn": fl.get("ИНН"),
                               "share_pct": _dig(fl, "Доля", "Процент"), "role": "founder"})
        ruk = d.get("Руковод") or []
        for r in (ruk if isinstance(ruk, list) else [ruk]):
            if r.get("ИНН") or r.get("ФИО"):
                owners.append({"name": r.get("ФИО"), "inn": r.get("ИНН"),
                               "share_pct": None, "role": "manager"})
        predecessors = [{"inn": p.get("ИНН"), "ogrn": p.get("ОГРН"), "name": p.get("НаимПолн")}
                        for p in (d.get("Правопредш") or [])]
        # Попытка найти другие компании учредителей-ФЛ (best-effort).
        related = []
        for o in owners:
            if o["role"] != "founder" or not o.get("inn"):
                continue
            res = self._data(src.request("search_founder", founder=o["inn"]))
            recs = (res or {}).get("Записи") or (res or {}).get("records") if isinstance(res, dict) else None
            for rec in (recs or []):
                rinn = rec.get("ИНН")
                if rinn and rinn != inn:
                    related.append({"owner": o["name"], "inn": rinn,
                                    "name": rec.get("НаимСокр") or rec.get("НаимПолн")})
        return {"owners": owners, "predecessors": predecessors, "related_companies": related}

    def financials(self, src: Source, inn: str) -> dict:
        raw = self._data(src.request("finances", inn=inn))
        if not isinstance(raw, dict):
            return {}
        years = {}
        for yr, codes in raw.items():
            if not str(yr).isdigit() or not isinstance(codes, dict):
                continue  # строки вида "Недоступно для бесплатного тарифа" -> пропуск
            parsed = _from_rsbu_codes(codes)
            if any(v is not None for v in parsed.values()):
                years[str(yr)] = parsed
        if not years:
            return {}  # на бесплатном тарифе финансы возьмёт ГИР БО (fallback)
        out = _empty_financials(inn)
        out["years"] = dict(sorted(years.items(), reverse=True))
        out["latest_year"] = max(years.keys())
        return out

    def risk_flags(self, src: Source, inn: str) -> dict:
        out = _empty_risk(inn)
        d = self._company_raw(src, inn)
        if d:
            out["status"] = _norm_status(_dig(d, "Статус", "Код"), _dig(d, "Статус", "Наим"))
            efrsb = d.get("ЕФРСБ")
            out["is_bankrupt"] = bool(efrsb) if efrsb not in (None,) else (out["status"] == "BANKRUPT")
            out["is_liquidating"] = out["status"] == "LIQUIDATING"
            lic = d.get("Лиценз")
            if isinstance(lic, list) and lic:
                out["licenses"] = [{"num": x.get("Номер"), "org": x.get("ЛицОрг"),
                                    "activities": x.get("ВидДеят")} for x in lic]
            neg = []
            if d.get("НедобПост"):
                neg.append("РНП (недобросовестный поставщик)")
            if d.get("Санкции") or d.get("СанкцУчр"):
                neg.append("санкционные списки")
            if d.get("МассРуковод"):
                neg.append("массовый руководитель")
            if d.get("МассУчред"):
                neg.append("массовый учредитель")
            if d.get("ДисквЛица"):
                neg.append("дисквалифицированные лица")
            out["negative_registries"] = neg or None
            nalogi = d.get("Налоги")
            if isinstance(nalogi, dict):
                out["tax_debt"] = _to_float(nalogi.get("СумНедоим") or nalogi.get("Задолж"))
        # арбитраж: список записей с ролью Ист/Ответ
        lc = self._data(src.request("legal_cases", inn=inn))
        if isinstance(lc, dict):
            recs = lc.get("Записи") or []
            d_cnt = p_cnt = 0
            d_sum = p_sum = 0.0
            inn_s = str(inn)
            for r in recs:
                is_def = any(str(x.get("ИНН")) == inn_s for x in (r.get("Ответ") or []))
                is_pl = any(str(x.get("ИНН")) == inn_s for x in (r.get("Ист") or []))
                s = _to_float(r.get("СуммИск")) or 0.0
                if is_def:
                    d_cnt += 1; d_sum += s
                if is_pl:
                    p_cnt += 1; p_sum += s
            out["legal_cases"] = {
                "total_count": _to_float(lc.get("ЗапВсего")),
                "total_sum": _to_float(lc.get("ОбщСуммИск")),
                "as_defendant_count": d_cnt, "as_defendant_sum": d_sum,
                "as_plaintiff_count": p_cnt, "as_plaintiff_sum": p_sum,
                "note": "роли посчитаны по доступной странице записей",
            }
        # ФССП
        enf = self._data(src.request("enforcements", inn=inn))
        if isinstance(enf, dict):
            out["enforcements"] = {
                "count": _to_float(enf.get("ОбщКолич") or enf.get("ЗапВсего")),
                "sum": _to_float(enf.get("ОбщСум")),
                "outstanding": _to_float(enf.get("ОстЗадолж")),
            }
        # госконтракты (требуется параметр law=44/223)
        gc_count = gc_sum = 0.0
        got_contracts = False
        for law in ("44", "223"):
            con = self._data(src.request("contracts", inn=inn, law=law))
            if isinstance(con, dict) and con:
                got_contracts = True
                gc_count += _to_float(con.get("ЗапВсего") or con.get("ОбщКолич")) or 0.0
                gc_sum += _to_float(con.get("ОбщСум") or con.get("ОбщСумма")) or 0.0
        if got_contracts:
            out["gov_contracts"] = {"count": gc_count, "sum": gc_sum}
        return out


# ---- ГИР БО (официальная бухотчётность, бесплатно) ------------------------
# Коды РСБУ -> канон. Используется и OFData, и Checko, и ГИР БО.
def _from_rsbu_codes(codes: Dict[str, Any]) -> dict:
    """codes: {'2110': value, '1600': value, ...} -> канонический год."""
    g = lambda c: _to_float(codes.get(c) if codes.get(c) is not None else codes.get(str(c)))
    y = _empty_year()
    y["revenue"] = g("2110")
    y["gross_profit"] = g("2100")
    y["ebit"] = g("2200")                       # прибыль (убыток) от продаж
    y["net_profit"] = g("2400")
    y["interest_paid"] = g("2330")
    y["assets"] = g("1600")
    y["equity"] = g("1300")
    long_debt = g("1410") or 0.0
    short_debt = g("1510") or 0.0
    y["debt"] = (long_debt + short_debt) or None          # процентный долг
    lt = g("1400") or 0.0
    st = g("1500") or 0.0
    y["total_liabilities"] = (lt + st) or None
    y["cash"] = g("1250")
    if y["debt"] is not None:
        y["net_debt"] = y["debt"] - (y["cash"] or 0.0)
    # амортизация в осн. формах отсутствует -> EBITDA = None (см. методику, fallback)
    y["depreciation"] = g("5640") or g("2410d")  # обычно None
    if y["ebit"] is not None and y["depreciation"] is not None:
        y["ebitda"] = y["ebit"] + y["depreciation"]
    return y


class GirBoParser:
    """
    bo.nalog.gov.ru — официальный ресурс БФО. Проверено живыми запросами.
    search -> org_id; bfo/ -> список выписок, каждая = 3 года (тек/пред/позапред).
    """

    @staticmethod
    def _find_org_id(src: Source, inn: str):
        raw = src.request("search", inn=inn)
        content = _dig(raw, "content") if isinstance(raw, dict) else None
        if not content:
            return None, None
        first = content[0]
        return first.get("id"), first

    def company_info(self, src: Source, inn: str) -> dict:
        org_id, first = self._find_org_id(src, inn)
        if not first:
            return {}
        out = _empty_company(inn)
        out["ogrn"] = first.get("ogrn")
        out["name_short"] = re.sub(r"</?strong>", "", str(first.get("shortName") or "")) or None
        out["okved_code"] = first.get("okved2")
        out["region"] = first.get("region")
        out["status"] = _norm_status(first.get("statusCode"), None)
        out["status_text"] = first.get("statusCode")
        addr = ", ".join(str(first.get(k)) for k in ("index", "region", "city", "street", "house")
                         if first.get(k))
        out["address"] = addr or None
        return out

    def financials(self, src: Source, inn: str) -> dict:
        org_id, _ = self._find_org_id(src, inn)
        if not org_id:
            return {}
        bfo = src.request("bfo", org_id=org_id)
        if not isinstance(bfo, list) or not bfo:
            return {}
        years: Dict[str, dict] = {}
        # каждая выписка: typeCorrections[].correction.{balance, finResult}
        for filing in bfo:
            period = str(filing.get("period") or "")
            corrs = filing.get("typeCorrections") or []
            balance, finres = {}, {}
            for c in corrs:
                corr = (c or {}).get("correction") or {}
                balance = corr.get("balance") or balance
                # ОФР (форма 0710002) лежит в ключе financialResult
                finres = corr.get("financialResult") or corr.get("finResult") or finres
            if not (balance or finres) or not period.isdigit():
                continue
            # собрать коды текущего/пред/позапрошлого года из суффиксов
            for suffix, shift in (("current", 0), ("previous", 1), ("beforePrevious", 2)):
                yr = str(int(period) - shift)
                codes = {}
                for blk in (balance, finres):
                    for k, v in (blk or {}).items():
                        m = re.match(rf"^{suffix}(\d{{4}})$", k)
                        if m:
                            codes[m.group(1)] = v
                if codes:
                    parsed = _from_rsbu_codes(codes)
                    # приоритет у более свежей выписки (current важнее previous из старой)
                    if yr not in years or shift == 0:
                        years[yr] = parsed
        if not years:
            return {}
        out = _empty_financials(inn)
        out["years"] = dict(sorted(years.items(), reverse=True))
        out["latest_year"] = max(years.keys())
        return out


# ---- Dadata (company_info) -------------------------------------------------
class DadataParser:
    def company_info(self, src: Source, inn: str) -> dict:
        raw = src.request("find", inn=inn)
        suggestions = _dig(raw, "suggestions") if isinstance(raw, dict) else None
        if not suggestions:
            return {}
        d = suggestions[0].get("data", {})
        out = _empty_company(inn)
        out["ogrn"] = d.get("ogrn")
        out["kpp"] = d.get("kpp")
        out["name_full"] = _dig(d, "name", "full_with_opf")
        out["name_short"] = _dig(d, "name", "short_with_opf")
        out["status"] = _norm_status(_dig(d, "state", "status"), None)
        reg = _dig(d, "state", "registration_date")
        out["registration_date"] = _ts_to_date(reg)
        out["okved_code"] = d.get("okved")
        out["okopf"] = _dig(d, "opf", "short")
        out["address"] = _dig(d, "address", "value")
        out["region"] = _dig(d, "address", "data", "region_with_type")
        mgmt = d.get("management") or {}
        if mgmt:
            out["manager"] = {"name": mgmt.get("name"), "post": mgmt.get("post")}
        out["employees"] = _to_float(d.get("employee_count"))
        cap = _dig(d, "capital", "value")
        out["authorized_capital"] = _to_float(cap)
        return out

    def risk_flags(self, src: Source, inn: str) -> dict:
        raw = src.request("find", inn=inn)
        suggestions = _dig(raw, "suggestions") if isinstance(raw, dict) else None
        if not suggestions:
            return {}
        d = suggestions[0].get("data", {})
        out = _empty_risk(inn)
        status = _norm_status(_dig(d, "state", "status"), None)
        out["status"] = status
        out["is_bankrupt"] = (status == "BANKRUPT") if status else None
        out["is_liquidating"] = (status == "LIQUIDATING") if status else None
        return out


def _ts_to_date(ts) -> Optional[str]:
    if ts is None:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(int(ts) / 1000).date().isoformat()
    except Exception:
        return None


# ---- ЕГРЮЛ (статус/факт существования, бесплатно) -------------------------
class EgrulParser:
    def company_info(self, src: Source, inn: str) -> dict:
        raw = src.request("search", inn=inn)
        if not isinstance(raw, dict):
            return {}
        out = _empty_company(inn)
        # ответ POST: {"t": token, "captchaRequired": false} — факт наличия записи
        if raw.get("t") and not raw.get("captchaRequired"):
            out["status"] = "ACTIVE"   # запись найдена; детальный статус — из выписки
            out["status_text"] = "запись в ЕГРЮЛ найдена (egrul.nalog.ru)"
        return out

    def risk_flags(self, src: Source, inn: str) -> dict:
        raw = src.request("search", inn=inn)
        out = _empty_risk(inn)
        if isinstance(raw, dict) and raw.get("t"):
            out["status"] = "ACTIVE"
        return out


# ---- Checko (агрегатор: API при ключе, иначе scrape HTML) -----------------
class CheckoParser:
    @staticmethod
    def _data(raw):
        if isinstance(raw, dict):
            if raw.get("meta", {}).get("status") == "error":
                return None
            return raw.get("data", raw)
        return None

    def financials(self, src: Source, inn: str) -> dict:
        if src.secret():
            d = self._data(src.request("finances", inn=inn))
            if d and isinstance(d, dict):
                years = {}
                container = d if all(str(k).isdigit() for k in d.keys()) else d.get("finances", {})
                for yr, codes in (container or {}).items():
                    if str(yr).isdigit() and isinstance(codes, dict):
                        years[str(yr)] = _from_rsbu_codes(codes)
                if years:
                    out = _empty_financials(inn)
                    out["years"] = years
                    out["latest_year"] = max(years.keys())
                    return out
        return {}  # scrape финансов опускаем (хрупко) — пусть отработает GIR BO

    def risk_flags(self, src: Source, inn: str) -> dict:
        if src.secret():
            d = self._data(src.request("company", inn=inn))
            if d:
                out = _empty_risk(inn)
                out["status"] = _norm_status(_dig(d, ("Статус", "status")), None)
                return out
        return {}


# ---- ФССП (исполнительные производства) ------------------------------------
class FsspParser:
    def risk_flags(self, src: Source, inn: str) -> dict:
        raw = src.request("search", inn=inn)
        recs = _dig(raw, "response", "result") if isinstance(raw, dict) else None
        if recs is None:
            return {}
        items = []
        if isinstance(recs, list):
            for blk in recs:
                items += (blk.get("result") or []) if isinstance(blk, dict) else []
        total = sum(_to_float(i.get("subject_sum") or i.get("sum")) or 0 for i in items)
        out = _empty_risk(inn)
        out["enforcements"] = {"count": len(items), "sum": total}
        return out


PARSERS: Dict[str, Any] = {
    "ofdata": OFDataParser(),
    "gir_bo": GirBoParser(),
    "dadata": DadataParser(),
    "egrul": EgrulParser(),
    "checko": CheckoParser(),
    "fssp": FsspParser(),
}


# ============================================================================
#  Движок: загрузка конфига, маршрутизация, сборка ответа
# ============================================================================
class Fetcher:
    def __init__(self, config_path: str = CONFIG_PATH):
        self.cfg = yaml.safe_load(Path(config_path).read_text("utf-8"))
        self.settings = self.cfg.get("settings", {})
        self.secrets_env = self.cfg.get("secrets", {})
        self.routing = self.cfg.get("routing", {})
        self.macro_cfg = self.cfg.get("macro", {})
        self.sources: Dict[str, Source] = {}
        for name, scfg in self.cfg.get("sources", {}).items():
            self.sources[name] = Source(name, scfg, self.settings, self.secrets_env)

    def _run_block(self, inn: str, block: str, method: str, empty_fn, fields) -> dict:
        result = empty_fn(inn)
        for sname in self.routing.get(block, []):
            src = self.sources.get(sname)
            if not src or not src.available():
                continue
            parser = PARSERS.get(src.parser_name)
            fn = getattr(parser, method, None) if parser else None
            if fn is None:
                continue
            try:
                partial = fn(src, inn) or {}
            except Exception as e:
                log.warning("parser %s.%s failed: %s", src.parser_name, method, e)
                partial = {}
            if not partial:
                continue
            # для финансов — особый merge по годам
            if block == "financials":
                self._merge_financials(result, partial)
                if partial.get("years"):
                    result["sources_used"].append(sname)
            else:
                used_before = {k: result.get(k) for k in fields}
                _merge_fill(result, {k: v for k, v in partial.items()
                                     if k not in ("sources_used", "data_quality")})
                if any(result.get(k) != used_before.get(k) for k in fields):
                    result["sources_used"].append(sname)
                # «ранний стоп»: если карточка уже достаточно полна из дешёвого
                # источника — не дёргать дорогой OFData (экономия лимита 100/сут).
                stop_q = self.settings.get("company_info_stop_quality", 0.0) or 0.0
                if block == "company_info" and stop_q > 0 and _quality(result, fields) >= stop_q:
                    break
        # качество
        if block == "financials":
            result["data_quality"] = self._fin_quality(result)
        else:
            result["data_quality"] = _quality(result, fields)
        return result

    @staticmethod
    def _merge_financials(dst: dict, src: dict) -> None:
        for yr, ydata in (src.get("years") or {}).items():
            if yr not in dst["years"]:
                dst["years"][yr] = _empty_year()
            _merge_fill(dst["years"][yr], ydata)
        if dst["years"]:
            dst["latest_year"] = max(dst["years"].keys())

    @staticmethod
    def _fin_quality(d: dict) -> float:
        years = d.get("years") or {}
        if not years:
            return 0.0
        # качество = среднее по годам, но достаточно 3 свежих лет
        recent = sorted(years.keys(), reverse=True)[:3]
        scores = [_quality(years[y], FIN_FIELDS) for y in recent]
        coverage = min(len(recent), 3) / 3.0
        base = sum(scores) / len(scores) if scores else 0.0
        return round(base * coverage, 3)

    # --- публичные методы --------------------------------------------------
    def company_info(self, inn: str) -> dict:
        if is_ip(inn):
            return self._company_info_ip(inn)
        return self._run_block(inn, "company_info", "company_info", _empty_company, COMPANY_FIELDS)

    def _company_info_ip(self, inn: str) -> dict:
        """ИП (12-значный ИНН): нет бухотчётности, тянем ЕГРИП через OFData/Dadata."""
        result = _empty_company(inn)
        result["is_ip"] = True
        for sname in self.routing.get("company_info", []):
            src = self.sources.get(sname)
            if not src or not src.available():
                continue
            parser = PARSERS.get(src.parser_name)
            fn = getattr(parser, "entrepreneur", None) or getattr(parser, "company_info", None)
            if not fn:
                continue
            try:
                partial = fn(src, inn) or {}
            except Exception as e:
                log.warning("ip parser %s failed: %s", sname, e)
                partial = {}
            if partial:
                _merge_fill(result, {k: v for k, v in partial.items()
                                     if k not in ("sources_used", "data_quality")})
                if sname not in result["sources_used"]:
                    result["sources_used"].append(sname)
        # для ИП качество считаем по доступным полям (без финансов/руковод)
        ip_fields = {k: v for k, v in COMPANY_FIELDS.items() if k not in ("manager",)}
        result["data_quality"] = _quality(result, ip_fields)
        return result

    def financials(self, inn: str) -> dict:
        return self._run_block(inn, "financials", "financials", _empty_financials, FIN_FIELDS)

    def risk_flags(self, inn: str) -> dict:
        out = self._run_block(inn, "risk_flags", "risk_flags", _empty_risk, RISK_FIELDS)
        self._derive_flags(out)
        return out

    @staticmethod
    def _derive_flags(out: dict) -> None:
        flags: List[dict] = []
        if out.get("status") == "BANKRUPT" or out.get("is_bankrupt"):
            flags.append({"code": "BANKRUPT", "severity": "critical", "text": "Банкротство / конкурсное производство"})
        if out.get("status") == "LIQUIDATING" or out.get("is_liquidating"):
            flags.append({"code": "LIQUIDATING", "severity": "high", "text": "В стадии ликвидации"})
        lc = out.get("legal_cases") or {}
        if (lc.get("as_defendant_sum") or 0) > 0:
            flags.append({"code": "LEGAL_DEFENDANT", "severity": "medium",
                          "text": f"Иски как ответчик: {lc.get('as_defendant_sum')}"})
        enf = out.get("enforcements") or {}
        if (enf.get("sum") or 0) > 0:
            flags.append({"code": "FSSP", "severity": "high",
                          "text": f"Исполнительные производства: {enf.get('sum')}"})
        if (out.get("tax_debt") or 0) > 0:
            flags.append({"code": "TAX_DEBT", "severity": "medium",
                          "text": f"Налоговая задолженность: {out.get('tax_debt')}"})
        ab = out.get("account_blocks") or {}
        if ab.get("active"):
            flags.append({"code": "ACCOUNT_BLOCK", "severity": "high", "text": "Блокировка счетов"})
        out["flags"] = flags
        # простой risk_score: взвешенная сумма серьёзности
        weight = {"critical": 1.0, "high": 0.5, "medium": 0.25, "low": 0.1}
        score = min(1.0, sum(weight.get(f["severity"], 0) for f in flags))
        out["risk_score"] = round(score, 3) if flags else 0.0

    # --- макро для DCF -----------------------------------------------------
    def macro(self) -> dict:
        out = {"key_rate": None, "ofz_10y": None, "source": None,
               "fallback_used": False, "as_of": None}
        cbr = self.sources.get("cbr")
        if cbr and cbr.available():
            try:
                html = cbr.request("key_rate", expect="text")
                if html:
                    m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>\s*([\d,]+)", html)
                    if m:
                        out["key_rate"] = _to_float(m.group(2))
                        out["as_of"] = m.group(1)
                        out["source"] = "cbr"
                zhtml = cbr.request("zcyc", expect="text")
                if zhtml:
                    # период 10 лет в кривой ZCYC
                    m2 = re.search(r"10[,\.]0?\s*</td>\s*<td[^>]*>\s*([\d,]+)", zhtml)
                    if m2:
                        out["ofz_10y"] = _to_float(m2.group(1))
            except Exception as e:
                log.warning("macro fetch failed: %s", e)
        if out["key_rate"] is None:
            out["key_rate"] = self.macro_cfg.get("fallback_key_rate")
            out["fallback_used"] = True
        if out["ofz_10y"] is None:
            out["ofz_10y"] = self.macro_cfg.get("fallback_ofz_10y")
            out["fallback_used"] = True
        return out

    # --- связанные компании (консолидация группы) --------------------------
    def affiliates(self, inn: str) -> dict:
        out = {"inn": inn, "owners": [], "predecessors": [], "related_companies": [],
               "sources_used": [], "note": None}
        for sname in self.routing.get("company_info", []):
            src = self.sources.get(sname)
            if not src or not src.available():
                continue
            parser = PARSERS.get(src.parser_name)
            fn = getattr(parser, "affiliates", None) if parser else None
            if not fn:
                continue
            try:
                part = fn(src, inn) or {}
            except Exception as e:
                log.warning("affiliates %s failed: %s", sname, e)
                part = {}
            if part:
                out["owners"] = out["owners"] or part.get("owners", [])
                out["predecessors"] = out["predecessors"] or part.get("predecessors", [])
                out["related_companies"] += part.get("related_companies", [])
                out["sources_used"].append(sname)
        if not out["related_companies"]:
            out["note"] = ("Связанные компании учредителей не получены автоматически "
                           "(search по учредителю недоступен/пуст). Проверить вручную: "
                           "Rusprofile/List-org/Checko по ИНН учредителя.")
        return out

    # --- сигналы масштаба (триангуляция истинной выручки/серого) -----------
    def scale_signals(self, inn: str, ci: dict = None, fin: dict = None) -> dict:
        """Сравнивает официальную выручку с implied-оценкой по числу сотрудников
        и отраслевым бенчмаркам -> флаг занижения/серого. Прочие сигналы
        (маркетплейс, импорт) подключаются при наличии данных."""
        ci = ci or self.company_info(inn)
        fin = fin or self.financials(inn)
        bench = _benchmark_for(ci.get("okved_code"))
        emp = ci.get("employees")
        years = fin.get("years") or {}
        latest = fin.get("latest_year")
        official_rev = (years.get(latest) or {}).get("revenue") if latest else None
        # ГИР БО отдаёт суммы в ТЫСЯЧАХ рублей -> приводим к рублям для сравнения
        if official_rev is not None and (fin.get("unit") == "thousand"):
            official_rev = official_rev * 1000
        rpe = bench.get("revenue_per_employee_rub_year") or [None, None, None]
        out = {
            "inn": inn, "okved": ci.get("okved_code"),
            "official_revenue": official_rev, "employees": emp,
            "benchmark_rev_per_employee": rpe,
            "implied_revenue_by_employees": None,
            "official_to_implied_ratio": None,
            "gray_flag": None, "implied_true_revenue": None, "note": None,
        }
        if emp and rpe[1]:
            implied = emp * rpe[1]
            out["implied_revenue_by_employees"] = implied
            if official_rev:
                ratio = official_rev / implied
                out["official_to_implied_ratio"] = round(ratio, 2)
                thr = _BENCH.get("triangulation_rules", {})
                strong = thr.get("gray_strong_threshold", 0.4)
                weak = thr.get("gray_flag_threshold", 0.6)
                if ratio < strong:
                    out["gray_flag"] = "strong"
                elif ratio < weak:
                    out["gray_flag"] = "moderate"
                else:
                    out["gray_flag"] = "none"
                # грубая оценка истинной выручки = max(официальная, implied по сотрудникам)
                out["implied_true_revenue"] = max(official_rev, implied)
        out["note"] = ("Эвристика по сотрудникам+бенчмарку. Для точной триангуляции "
                       "добавить: маркетплейс (Moneyplace/MPStats), импорт (таможня), "
                       "число клиентов*чек, банковские обороты (DD).")
        return out


# ============================================================================
#  Бенчмарки (valuation_benchmarks.yaml)
# ============================================================================
_BENCH: dict = {}


def _load_benchmarks() -> dict:
    global _BENCH
    if _BENCH:
        return _BENCH
    p = Path(__file__).with_name("valuation_benchmarks.yaml")
    if not p.exists():                                   # приватной нет → публичный пример
        p = Path(__file__).with_name("valuation_benchmarks.example.yaml")
    if p.exists():
        try:
            _BENCH = yaml.safe_load(p.read_text("utf-8")) or {}
        except Exception as e:
            log.warning("benchmarks load failed: %s", e)
            _BENCH = {}
    return _BENCH


# карта ОКВЭД-префиксов -> ключ бенчмарка (синхронно с valuation_params.multiples)
_OKVED_MAP = {
    "58": "it_software", "61": "it_software", "62": "it_software", "63": "it_software",
    "47": "retail", "46": "wholesale", "56": "food_service",
    "41": "construction", "42": "construction", "43": "construction",
    "49": "transport_logistics", "50": "transport_logistics", "51": "transport_logistics",
    "52": "transport_logistics", "53": "transport_logistics",
    "86": "healthcare", "21": "pharma",
    "85": "education", "68": "real_estate",
    "01": "agriculture", "02": "agriculture", "03": "agriculture",
    "93": "consumer_services", "95": "consumer_services", "96": "consumer_services",
}
_OKVED_MAP.update({f"{n:02d}": "manufacturing" for n in
                   [10, 11, 13, 14, 15, 16, 17, 18, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33]})
_OKVED_MAP.update({f"{n:02d}": "professional_services" for n in
                   [69, 70, 71, 73, 74, 78, 82]})


def _benchmark_for(okved: Optional[str]) -> dict:
    b = _load_benchmarks().get("benchmarks", {})
    if okved:
        key = _OKVED_MAP.get(str(okved).split(".")[0].zfill(2))
        if key and key in b:
            return b[key]
    return b.get("default", {})


def is_ip(inn: str) -> bool:
    """ИП/физлицо: 12-значный ИНН. Юрлицо: 10-значный."""
    return len(str(inn).strip()) == 12


# ============================================================================
#  Модульный фасад (фиксированный интерфейс)
# ============================================================================
_DEFAULT: Optional[Fetcher] = None


def _default() -> Fetcher:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Fetcher()
    return _DEFAULT


def get_company_info(inn: str) -> dict:
    return _default().company_info(str(inn).strip())


def get_financials(inn: str) -> dict:
    return _default().financials(str(inn).strip())


def get_risk_flags(inn: str) -> dict:
    return _default().risk_flags(str(inn).strip())


def get_macro() -> dict:
    return _default().macro()


def get_affiliates(inn: str) -> dict:
    return _default().affiliates(str(inn).strip())


def get_scale_signals(inn: str) -> dict:
    return _default().scale_signals(str(inn).strip())


def get_benchmark(okved: str) -> dict:
    return _benchmark_for(okved)


def fetch_all(inn: str) -> dict:
    f = _default()
    inn = str(inn).strip()
    ci = f.company_info(inn)
    fin = f.financials(inn)
    return {
        "company_info": ci,
        "financials": fin,
        "risk_flags": f.risk_flags(inn),
        "affiliates": f.affiliates(inn),
        "scale_signals": f.scale_signals(inn, ci=ci, fin=fin),
        "benchmark": _benchmark_for(ci.get("okved_code")),
        "macro": f.macro(),
    }


# ============================================================================
#  CLI: python data_fetcher.py <ИНН> [block]
# ============================================================================
if __name__ == "__main__":
    import sys
    inn_arg = sys.argv[1] if len(sys.argv) > 1 else "7736207543"
    block_arg = sys.argv[2] if len(sys.argv) > 2 else "all"
    funcs = {
        "company": get_company_info, "financials": get_financials,
        "risk": get_risk_flags, "macro": lambda _: get_macro(),
        "affiliates": get_affiliates, "scale": get_scale_signals, "all": fetch_all,
    }
    fn = funcs.get(block_arg, fetch_all)
    res = fn(inn_arg)
    print(json.dumps(res, ensure_ascii=False, indent=2))
