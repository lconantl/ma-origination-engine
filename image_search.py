# -*- coding: utf-8 -*-
"""
image_search.py — поиск изображений по названию/бренду компании для тизера.

Решение проблемы «сайт за антиботом»: вместо борьбы с DDoS-Guard на сайте компании
ищем фото по БРЕНДУ (имя берём из реестра data_fetcher) в открытом источнике картинок,
скачиваем N кандидатов, а агент (зрение Claude) отбирает самые релевантные под слайды.

Источники (tiered):
  1) DuckDuckGo Images — БЕЗ КЛЮЧА, бесплатно (основной).
  2) Google Custom Search / SerpAPI — если задан ключ (надёжнее, опционально).

Использование:
    import image_search as imgs
    res  = imgs.search_images("Кармин-Авто автозапчасти грузовые", n=15)
    paths = imgs.download_images(res, outdir="photos", limit=12)
    # дальше агент смотрит paths ЗРЕНИЕМ и выбирает под обложку/продукт/команду
"""
from __future__ import annotations
import re, io, os, time, hashlib
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_session = requests.Session()
_session.headers.update({"User-Agent": UA})

# нерелевантные домены/источники (стоки, бэйджи, агрегаторы-помойки)
_BAD_HOST = re.compile(r"(shutterstock|istockphoto|alamy|123rf|dreamstime|gettyimages|"
                       r"favicon|sprite|logo-)", re.I)


# ---------- DuckDuckGo (без ключа) ----------
def _ddg_vqd(query: str):
    try:
        r = _session.get("https://duckduckgo.com/", params={"q": query}, timeout=20)
        m = re.search(r'vqd=["\']?([\d-]+)', r.text)
        return m.group(1) if m else None
    except Exception:
        return None


def _search_ddg(query: str, n: int):
    vqd = _ddg_vqd(query)
    if not vqd:
        return []
    try:
        r = _session.get("https://duckduckgo.com/i.js",
                         params={"l": "ru-ru", "o": "json", "q": query, "vqd": vqd,
                                 "f": ",,,", "p": "1"},
                         headers={"Referer": "https://duckduckgo.com/"}, timeout=20)
        results = r.json().get("results", [])
    except Exception:
        return []
    out = []
    for it in results:
        url = it.get("image")
        if not url or _BAD_HOST.search(url):
            continue
        out.append({"url": url, "title": it.get("title", ""),
                    "w": it.get("width"), "h": it.get("height"),
                    "thumb": it.get("thumbnail"), "source": it.get("url", "")})
    return out[: n * 3]  # с запасом — часть отвалится при скачивании


# ---------- Google CSE / SerpAPI (опционально, по ключу) ----------
def _search_google(query: str, n: int):
    key = os.environ.get("GOOGLE_API_KEY"); cx = os.environ.get("GOOGLE_CX")
    if not (key and cx):
        return []
    try:
        r = _session.get("https://www.googleapis.com/customsearch/v1",
                         params={"key": key, "cx": cx, "q": query, "searchType": "image",
                                 "num": min(10, n), "gl": "ru", "hl": "ru"}, timeout=20)
        items = r.json().get("items", [])
        return [{"url": it["link"], "title": it.get("title", ""),
                 "w": it.get("image", {}).get("width"), "h": it.get("image", {}).get("height"),
                 "source": it.get("image", {}).get("contextLink", "")} for it in items]
    except Exception:
        return []


def search_images(query: str, n: int = 15, min_w: int = 400, min_h: int = 300,
                  brand_boost: str = "") -> list[dict]:
    """Ищет изображения по запросу. Возвращает кандидатов (url, title, размеры, source),
    отсортированных по релевантности (совпадение бренда в title + размер)."""
    res = _search_google(query, n) or _search_ddg(query, n)
    # фильтр по размеру (если известен)
    filt = []
    for it in res:
        try:
            if it.get("w") and it.get("h") and (int(it["w"]) < min_w or int(it["h"]) < min_h):
                continue
        except (ValueError, TypeError):
            pass
        filt.append(it)
    # дедуп по домену (не более 3 с одного хоста)
    from urllib.parse import urlparse
    seen = {}
    dedup = []
    for it in filt:
        host = urlparse(it["url"]).netloc
        if seen.get(host, 0) >= 3:
            continue
        seen[host] = seen.get(host, 0) + 1
        dedup.append(it)
    # скоринг: бренд в title -> выше
    bb = (brand_boost or "").lower()
    for it in dedup:
        sc = 0.0
        if bb and bb in (it.get("title", "").lower()):
            sc += 5
        try:
            sc += min(int(it.get("w") or 0) * int(it.get("h") or 0) / 1e6, 4)
        except (ValueError, TypeError):
            pass
        it["score"] = round(sc, 2)
    dedup.sort(key=lambda x: x["score"], reverse=True)
    return dedup[:n]


def download_images(results: list[dict], outdir="photos", limit: int = 12,
                    min_bytes: int = 8000) -> list[dict]:
    """Скачивает изображения, проверяет что открываются (PIL), отсеивает битые/мелкие.
    Возвращает [{path, url, title, w, h}]."""
    try:
        from PIL import Image
    except Exception:
        Image = None
    Path(outdir).mkdir(parents=True, exist_ok=True)
    saved = []
    for it in results:
        if len(saved) >= limit:
            break
        try:
            r = _session.get(it["url"], timeout=20)
            if r.status_code >= 400 or len(r.content) < min_bytes:
                continue
            data = r.content
            w = h = None
            if Image:
                try:
                    im = Image.open(io.BytesIO(data)); w, h = im.size
                    if w < 350 or h < 250:
                        continue
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    ext = "jpg"
                    fn = Path(outdir) / (hashlib.sha1(it["url"].encode()).hexdigest()[:12] + "." + ext)
                    im.save(fn, quality=88)
                except Exception:
                    continue
            else:
                fn = Path(outdir) / (hashlib.sha1(it["url"].encode()).hexdigest()[:12] + ".img")
                fn.write_bytes(data)
            saved.append({"path": str(fn), "url": it["url"], "title": it.get("title", ""),
                          "w": w, "h": h, "score": it.get("score")})
        except Exception:
            continue
        time.sleep(0.2)
    return saved


def brand_query(company_name: str, okved_name: str = "", extra: str = "") -> str:
    """Строит поисковый запрос из названия компании (из реестра) + контекста отрасли."""
    name = re.sub(r'^(ООО|АО|ПАО|ЗАО|ИП)\s*["«]?|["»]', "", company_name or "").strip()
    return " ".join(x for x in [name, okved_name, extra] if x).strip()


if __name__ == "__main__":
    import sys, json
    q = sys.argv[1] if len(sys.argv) > 1 else "Кармин-Авто автозапчасти грузовые"
    res = search_images(q, n=15, brand_boost="кармин")
    print(f"Найдено кандидатов: {len(res)} по запросу '{q}'")
    for it in res[:15]:
        print(f"  score={it.get('score')} {str(it.get('w'))}x{str(it.get('h'))} {it['title'][:40]:40} {it['url'][:60]}")
