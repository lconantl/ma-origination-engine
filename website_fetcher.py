# -*- coding: utf-8 -*-
"""
website_fetcher.py — сбор контента и ИЗОБРАЖЕНИЙ с сайта компании для тизера.

Возвращает структуру:
  {
    "url", "title", "description", "headings": [...], "text": "...",
    "products": [...], "contacts": {emails, phones, socials},
    "images": [ {url, alt, score, source, w, h}, ... ]   # отсортированы по пригодности
  }

ВАЖНО про фото (жёсткое требование): модуль СОБИРАЕТ и эвристически РАНЖИРУЕТ
кандидатов (og:image и крупные контентные фото — выше; иконки/логотипы/спрайты —
отсев). Финальный выбор «какое фото на какой слайд» делает агент через ЗРЕНИЕ Claude
(передаёшь ему top-N url -> он смотрит и подбирает под слайд).

Зависимости: requests, beautifulsoup4, lxml.
"""
from __future__ import annotations
import re
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError as e:  # pragma: no cover
    raise SystemExit("Нужен beautifulsoup4: pip install beautifulsoup4 lxml") from e

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
TIMEOUT = 20

# мусорные изображения (логотипы/иконки/спрайты/пиксели/соцкнопки)
_IMG_JUNK = re.compile(
    r"(logo|icon|sprite|favicon|pixel|tracking|placeholder|loader|spinner|"
    r"button|btn|arrow|social|vk\.|telegram|whatsapp|badge|stub|1x1|blank)", re.I)
_IMG_EXT_BAD = re.compile(r"\.(svg|gif)(\?|$)", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_SOCIAL = re.compile(r"(t\.me/|vk\.com/|instagram\.com/|youtube\.com/|wa\.me/|ok\.ru/)", re.I)


_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
_session = requests.Session()


def _is_antibot(html: str) -> bool:
    return bool(re.search(r"(проверка безопасности|just a moment|ddos-guard|cloudflare|"
                          r"checking your browser|captcha|enable javascript)", html[:2000], re.I))


def _get(url: str):
    try:
        r = _session.get(url, headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code < 400 and "text/html" in r.headers.get("Content-Type", "text/html"):
            if _is_antibot(r.text):
                return {"_antibot": True}
            return r.text
    except Exception:
        return None
    return None


def _get_rendered(url: str, max_wait_ms: int = 15000):
    """Fallback для антибот/SPA-сайтов: рендер через headless-браузер (Playwright).
    Проходит JS-челлендж DDoS-Guard/Cloudflare (ждёт, пока исчезнет экран проверки).
    Требует: pip install playwright && playwright install chromium.
    Если Playwright не установлен — возвращает None (модуль не падает)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled", "--no-sandbox"])
            ctx = browser.new_context(
                user_agent=UA, locale="ru-RU", viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"})
            # маскируем navigator.webdriver (антибот часто проверяет)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            # ждём, пока пройдёт челлендж: экран проверки исчезнет
            waited = 0
            while waited < max_wait_ms:
                page.wait_for_timeout(1000)
                waited += 1000
                try:
                    if not _is_antibot(page.content()):
                        break
                except Exception:
                    pass
            page.wait_for_timeout(1500)  # дать JS дорисовать контент
            html = page.content()
            browser.close()
            return None if _is_antibot(html) else html
    except Exception:
        return None


def _get_via_unblocker(url: str):
    """Тир-3 для ЖЁСТКОГО антибота (DDoS-Guard/Cloudflare), который не берёт Playwright.
    Использует платный API-разблокировщик (ScrapingBee/ZenRows-совместимый): рендерит
    JS + проходит антибот + резидентные IP. Ключ из env SCRAPER_API_KEY.
    База настраивается SCRAPER_API_BASE (по умолчанию ScrapingBee).
    Без ключа -> None (модуль не падает). ~$0.001–0.01 за запрос."""
    import os
    key = os.environ.get("SCRAPER_API_KEY")
    if not key:
        return None
    base = os.environ.get("SCRAPER_API_BASE", "https://app.scrapingbee.com/api/v1/")
    try:
        if "scrapingbee" in base:
            params = {"api_key": key, "url": url, "render_js": "true", "country_code": "ru"}
        elif "zenrows" in base:
            params = {"apikey": key, "url": url, "js_render": "true", "antibot": "true"}
        else:  # generic
            params = {"api_key": key, "url": url, "render": "true"}
        r = requests.get(base, params=params, timeout=70)
        if r.status_code < 400 and len(r.text) > 200 and not _is_antibot(r.text):
            return r.text
    except Exception:
        return None
    return None


def _score_image(src: str, alt: str, w, h, is_og: bool) -> float:
    score = 0.0
    if is_og:
        score += 5            # og:image — обычно главный визуал
    if alt and len(alt) > 3:
        score += 1
    # размер (если известен из атрибутов)
    try:
        if w and h:
            area = int(w) * int(h)
            if area >= 300 * 200:
                score += 3
            elif area < 80 * 80:
                score -= 4     # мелочь = иконка
    except (ValueError, TypeError):
        pass
    if _IMG_JUNK.search(src) or (alt and _IMG_JUNK.search(alt)):
        score -= 5
    if _IMG_EXT_BAD.search(src):
        score -= 3
    # контентные пути (product/gallery/photo/upload) — плюс
    if re.search(r"(product|catalog|gallery|photo|upload|content|media|image)", src, re.I):
        score += 2
    return score


def fetch_site(url: str, max_pages: int = 3) -> dict:
    """Сайт -> структурированный контент + ранжированные изображения."""
    if not url.startswith("http"):
        url = "https://" + url
    out = {"url": url, "title": None, "description": None, "headings": [],
           "text": "", "products": [], "contacts": {"emails": [], "phones": [], "socials": []},
           "images": [], "error": None, "pages_fetched": []}

    def _ok(h):
        return isinstance(h, str) and len(h) > 200

    def _thin(h):
        """Похоже на SPA/защиту: нет заголовка ИЛИ мало картинок."""
        if not _ok(h):
            return True
        nimg = h.lower().count("<img")
        has_title = bool(re.search(r"<title>\s*\S{4,}", h, re.I))
        return nimg < 3 or not has_title

    raw = _get(url)
    antibot = isinstance(raw, dict) and raw.get("_antibot")
    plain = raw if _ok(raw) else None
    if not plain:
        for alt_url in (url.replace("https://", "http://"),
                        url.replace("https://", "https://www.")):
            h2 = _get(alt_url)
            if isinstance(h2, dict) and h2.get("_antibot"):
                antibot = True
            if _ok(h2):
                plain, url = h2, alt_url
                break

    # Рендер через Playwright, если: антибот, нет html, ИЛИ контент «тонкий» (SPA).
    html = plain
    if antibot or _thin(plain):
        rendered = _get_rendered(url)
        if _ok(rendered) and not _thin(rendered):
            html, antibot = rendered, False
            out["render_mode"] = "playwright"
        elif _ok(rendered):
            html = rendered  # хоть что-то отрисовалось
            out["render_mode"] = "playwright"

    if not _ok(html):
        out["error"] = ("антибот/SPA: установи Playwright (pip install playwright && "
                        "playwright install chromium)" if antibot else "сайт недоступен")
        return out

    base = url
    images, texts, headings, products = {}, [], [], []

    def parse(page_html, page_url):
        soup = BeautifulSoup(page_html, "lxml")
        # title / description / og
        if not out["title"] and soup.title:
            out["title"] = soup.title.get_text(strip=True)
        for meta in soup.find_all("meta"):
            if meta.get("name", "").lower() == "description" and not out["description"]:
                out["description"] = meta.get("content")
            if meta.get("property", "").lower() == "og:image":
                u = urljoin(page_url, meta.get("content", ""))
                if u:
                    images[u] = {"url": u, "alt": out.get("title") or "", "w": None, "h": None,
                                 "is_og": True, "source": "og:image"}
        # headings / text
        for tag in soup.find_all(["h1", "h2", "h3"]):
            t = tag.get_text(" ", strip=True)
            if t and len(t) < 160:
                headings.append(t)
        for tag in soup.find_all(["p", "li"]):
            t = tag.get_text(" ", strip=True)
            if 20 < len(t) < 400:
                texts.append(t)
        # products (списки/карточки)
        for tag in soup.select("li, .product, .card, .catalog-item"):
            t = tag.get_text(" ", strip=True)
            if 3 < len(t) < 80 and not re.search(r"(меню|контакт|политик|cookie|©)", t, re.I):
                products.append(t)
        # images
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or src.startswith("data:"):
                continue
            u = urljoin(page_url, src)
            if u in images:
                continue
            images[u] = {"url": u, "alt": img.get("alt", ""), "w": img.get("width"),
                         "h": img.get("height"), "is_og": False, "source": "img"}
        # контакты
        body = soup.get_text(" ", strip=True)
        out["contacts"]["emails"] += _EMAIL.findall(body)
        out["contacts"]["phones"] += _PHONE.findall(body)
        for a in soup.find_all("a", href=True):
            if _SOCIAL.search(a["href"]):
                out["contacts"]["socials"].append(a["href"])
        # внутренние ссылки на about/products
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            if urlparse(href).netloc == urlparse(base).netloc and \
               re.search(r"(about|company|o-?nas|o-?kompanii|product|catalog|services|uslugi|brand)", href, re.I):
                links.append(href.split("#")[0])
        return links

    page_links = parse(html, url)
    out["pages_fetched"].append(url)
    # дотягиваем до max_pages релевантных внутренних страниц
    for link in list(dict.fromkeys(page_links))[: max_pages - 1]:
        ph = _get(link)
        if ph:
            parse(ph, link)
            out["pages_fetched"].append(link)

    # финализация
    out["headings"] = list(dict.fromkeys(headings))[:25]
    out["products"] = list(dict.fromkeys(products))[:40]
    out["text"] = " ".join(dict.fromkeys(texts))[:4000]
    out["contacts"]["emails"] = sorted(set(out["contacts"]["emails"]))[:5]
    out["contacts"]["phones"] = sorted(set(out["contacts"]["phones"]))[:5]
    out["contacts"]["socials"] = sorted(set(out["contacts"]["socials"]))[:8]
    # ранжируем изображения
    ranked = []
    for u, im in images.items():
        im["score"] = _score_image(u, im["alt"], im["w"], im["h"], im["is_og"])
        ranked.append(im)
    ranked.sort(key=lambda x: x["score"], reverse=True)
    out["images"] = [x for x in ranked if x["score"] > -3][:20]
    return out


if __name__ == "__main__":
    import sys, json
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.ozon.ru"
    res = fetch_site(url)
    print(f"URL: {res['url']} | страниц: {len(res['pages_fetched'])} | ошибка: {res['error']}")
    print(f"Заголовок: {res['title']}")
    print(f"Описание: {(res['description'] or '')[:160]}")
    print(f"H-заголовки ({len(res['headings'])}): {res['headings'][:6]}")
    print(f"Продукты/услуги ({len(res['products'])}): {res['products'][:8]}")
    print(f"Контакты: {res['contacts']}")
    print(f"\nИЗОБРАЖЕНИЯ (топ-{min(8,len(res['images']))} по пригодности):")
    for im in res["images"][:8]:
        print(f"  score={im['score']:+.0f} src={im['source']:8} alt='{(im['alt'] or '')[:30]}' {im['url'][:80]}")
