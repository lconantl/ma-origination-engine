# -*- coding: utf-8 -*-
"""
render_html.py — финальный рендер тизера: HTML/CSS-шаблон -> PDF (через Chromium).
Авто-вёрстка CSS grid/flex => цифры НЕ налезают, дизайн уровня Big3. Картинки и
графики встраиваются base64 (self-contained). Тема в THEME -> перекраска под бренд.

Зависимости: playwright (+ playwright install chromium). Уже установлены.
Контент — тот же словарь, что у render_pptx (teaser_schema): см. slide_playbook.md.
"""
from __future__ import annotations
import base64, html, tempfile
from pathlib import Path
import re, unicodedata

THEME = {
    "primary": "#12372A",
    "accent": "#19A974",
    "positive": "#19A974",
    "positive_bg": "#E8F6EF",
    "gold": "#C99A3D",
    "ink": "#17211C",
    "muted": "#718078",
    "hairline": "#DDE5E0",
    "bg": "#FFFFFF",
    "card": "#F3F7F4",
    "font": "'PT Sans', 'Helvetica Neue', Arial, sans-serif",
}

_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str, maxlen: int = 60) -> str:
    """ООО «Ромашка-Плюс» -> romashka-plyus"""
    s = str(name or "").lower()
    s = re.sub(r'^(ооо|оао|зао|пао|ао|ип)\b[\s"«»]*', "", s)  # срезать орг-форму
    s = "".join(_TR.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:maxlen].rstrip("-") or "teaser")


def _img_uri(path):
    if not path or not Path(str(path)).exists():
        return ""
    ext = Path(path).suffix.lstrip(".").lower() or "png"
    if ext in ("jpg", "jpeg"):
        mime = "image/jpeg"
    else:
        mime = "image/png"
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def _e(x):
    return html.escape(str(x)) if x is not None else ""


def _clip(x, n):
    """Жёстко ограничить длину текста (защита вёрстки от переполнения)."""
    if x is None:
        return ""
    s = str(x).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _bullets(items, cls="bul"):
    return "".join(f'<li class="{cls}">{_e(i)}</li>' for i in (items or []))


CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
@page { size: 1280px 720px; margin:0; }
body { font-family: %(font)s; color: %(ink)s; }
/* перенос длинных неразрывных токенов (URL/хеши) — не вылезают за колонку */
.atitle, .body, .bul, .prow .v, .card p, .kpi .val, .disc, h1, .sub, .cta { overflow-wrap:anywhere; word-break:break-word; }
.slide { width:1280px; height:720px; position:relative; background:%(bg)s;
         overflow:hidden; page-break-after:always; }
/* overflow:hidden — контент НИКОГДА не пересекает зону футера */
.pad { padding: 38px 54px 64px 54px; height:100%%; display:flex; flex-direction:column; overflow:hidden; }
.kicker { font-size:13px; letter-spacing:1.5px; text-transform:uppercase;
          color:%(muted)s; font-weight:700; }
.atitle { font-size:27px; line-height:1.25; color:%(primary)s; font-weight:800;
          margin-top:6px; max-width:1120px; }
.rule { height:2px; background:%(hairline)s; margin:16px 0 22px; }
.subhead {
    background:%(positive_bg)s;
    color:%(primary)s;
    border:1px solid #CBE8D8;
    font-weight:700;
    font-size:14px;
    padding:7px 13px;
    border-radius:6px;
    display:inline-block;
}
.body { font-size:15px; line-height:1.5; }
ul { list-style:none; }
.bul { font-size:15px; line-height:1.5; padding-left:20px; position:relative; margin:9px 0; }
.bul:before { content:""; width:6px; height:6px; border-radius:3px; background:%(accent)s; position:absolute; left:2px; top:.65em; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:34px; flex:1; }
.cols6040 { display:grid; grid-template-columns:1.5fr 1fr; gap:34px; flex:1; }
.footer { position:absolute; left:54px; right:54px; bottom:20px; display:flex;
          justify-content:space-between; font-size:11px; color:%(muted)s;
          border-top:1px solid %(hairline)s; padding-top:8px; }
img.chart { width:100%%; height:auto; max-height:300px; object-fit:contain; }
/* cover */
.cover { width:1280px; height:720px; position:relative; background:%(primary)s;
         overflow:hidden; page-break-after:always; }
.cover .hero { position:absolute; inset:0; width:100%%; height:100%%; object-fit:cover; }
.cover .veil { position:absolute; inset:0;
   background:linear-gradient(180deg, rgba(18,55,42,.20) 0%%, rgba(18,55,42,.55) 55%%, rgba(18,55,42,.94) 100%%); }
.cover .ct { position:absolute; left:60px; right:60px; bottom:70px; }
.cover .accentbar {
    width:72px;
    height:4px;
    background:%(accent)s;
    margin-bottom:22px;
    border-radius:2px;
}
.cover h1 { color:#fff; font-size:40px; line-height:1.18; font-weight:800; max-width:1000px; }
.cover .sub { color:%(accent)s; font-weight:700; letter-spacing:2px; text-transform:uppercase;
              font-size:16px; margin-top:18px; max-width:860px; }
.cover .date { color:#cdd6e2; font-size:13px; position:absolute; right:60px; bottom:74px; }
.cover .logo { position:absolute; top:48px; left:60px; max-height:60px; max-width:230px; }
/* kpi card */
.kpi {
    background:%(primary)s;
    border-radius:12px;
    padding:26px 26px;
    color:#fff;
    display:flex;
    flex-direction:column;
    justify-content:center;
    box-shadow:0 8px 24px rgba(18,55,42,.10);
}
.kpi h3 { color:%(accent)s; font-size:14px; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:14px; }
.kpi .row { margin:11px 0; }
.kpi .lbl {
    font-size:11px;
    color:#A9BDB3;
    text-transform:uppercase;
    letter-spacing:.7px;
}
.kpi .val {
    font-size:24px;
    font-weight:800;
    letter-spacing:-.3px;
}
/* profile rows */
.prow { display:flex; margin:13px 0; font-size:15px; }
.prow .k { width:150px; color:%(muted)s; font-weight:700; text-transform:uppercase; font-size:12px; padding-top:2px; }
.prow .v { flex:1; }
/* catalysts */
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:20px; flex:1; align-content:start; margin-top:8px; }
.card {
    background:%(card)s;
    border-radius:10px;
    padding:22px 18px;
    border-top:4px solid %(accent)s;
}
.card h4 {
    color:%(primary)s;
    font-size:17px;
    margin-bottom:12px;
    min-height:46px;
    line-height:1.25;
}
.card p { font-size:13.5px; line-height:1.45; }
/* valuation */
.disc { background:%(card)s; border-radius:8px; padding:16px 18px; font-size:11.5px;
        color:%(muted)s; line-height:1.5; margin-top:14px; }
.ev {
    background:%(primary)s;
    color:#fff;
    border-radius:12px;
    padding:20px 22px;
    margin-bottom:16px;
}

.ev .lbl {
    color:%(gold)s;
    font-size:12px;
    text-transform:uppercase;
    letter-spacing:1px;
}

.ev .val {
    font-size:32px;
    font-weight:800;
    margin-top:4px;
}
.imgband { width:100%%; border-radius:8px; max-height:230px; object-fit:cover; }
""" % THEME


def _footer(src, page):
    return (f'<div class="footer"><span>Источник: {_e(src)}</span>'
            f'<span>Конфиденциально</span><span>{page}</span></div>')


def _slides(c, charts, images):
    g = charts.get;
    img = images.get
    S = []
    # 1 cover
    hero = _img_uri(img("hero"))
    logo = f'<img class="logo" src="{_img_uri(img("logo"))}">' if img("logo") else ""
    S.append(f'''<div class="cover">
      {f'<img class="hero" src="{hero}">' if hero else ''}<div class="veil"></div>
      {logo}
      <div class="ct"><div class="accentbar"></div>
        <h1>{_e(c["cover"]["positioning"])}</h1>
        <div class="sub">{_e(c["cover"]["subtitle"])}</div></div>
      <div class="date">{_e(c["cover"].get("date", ""))}</div></div>''')
    # 2 exec summary
    sm = c["summary"];
    kf = sm["key_figures"]
    kpis = [("Выручка", kf.get("revenue")), ("EBITDA (норм.)", kf.get("ebitda")),
            ("Клиентская база", kf.get("clients")), ("Оценка EV", kf.get("ev_range"))]
    kpi_rows = "".join(f'<div class="row"><div class="lbl">{_e(l)}</div><div class="val">{_e(v)}</div></div>'
                       for l, v in kpis if v)
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(sm["action_title"], 180))}</div><div class="rule"></div>
      <div class="cols6040">
        <div><div class="body" style="margin-bottom:18px">{_e(_clip(sm["one_liner"], 360))}</div>
          <div class="subhead">Почему это привлекательно</div>
          <ul style="margin-top:12px">{_bullets([_clip(h, 125) for h in (sm["highlights"] or [])[:6]])}</ul></div>
        <div class="kpi"><h3>Ключевые показатели</h3>{kpi_rows}</div>
      </div>{_footer(sm.get("source", "ФНС / ГИР БО; анализ"), 2)}</div></div>''')
    # 3 company
    co = c["company"]
    rows = [("Основана", co.get("founded")), ("География", co.get("location")),
            ("Команда", co.get("team_size")), ("Бренды", ", ".join(co.get("brands") or [])),
            ("Модель", co.get("business_model"))]
    prows = "".join(f'<div class="prow"><div class="k">{_e(k)}</div><div class="v">{_e(_clip(v, 120))}</div></div>'
                    for k, v in rows if v)  # пустые значения не рендерим
    cimg = f'<img class="imgband" src="{_img_uri(img("company"))}" style="margin-bottom:14px">' if img(
        "company") else ""
    perim = [p for p in (co.get("deal_perimeter") or []) if str(p).strip()]
    bullets = [_clip(x, 130) for x in (co.get("exclusives") or [])]
    if perim:
        bullets.append(_clip("Периметр: " + ", ".join(perim), 150))
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(co["action_title"], 180))}</div><div class="rule"></div>
      <div class="cols">
        <div><div class="subhead">Профиль компании</div><div style="margin-top:14px">{prows}</div></div>
        <div>{cimg}<div class="subhead">Эксклюзивы и периметр</div>
          <ul style="margin-top:12px">{_bullets(bullets)}</ul></div>
      </div>{_footer("ЕГРЮЛ / ГИР БО; данные компании", 3)}</div></div>''')
    # 4 market
    mk = c["market"]
    risks = f'<div class="body" style="color:{THEME["muted"]};margin-top:14px;font-size:12px">Риски: {_e(_clip("; ".join(mk.get("risks") or []), 200))}</div>' if mk.get(
        "risks") else ""
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(mk["action_title"], 180))}</div><div class="rule"></div>
      <div class="cols">
        <div><div class="subhead">Динамика рынка</div>
          <img class="chart" src="{_img_uri(g("market"))}" style="margin-top:14px"></div>
        <div><div class="subhead">Драйверы</div>
          <ul style="margin-top:12px">{_bullets([_clip(t, 120) for t in (mk.get("tailwinds") or [])[:5]])}</ul>{risks}</div>
      </div>{_footer("; ".join(mk.get("sources", ["отраслевые отчёты"])[:3]), 4)}</div></div>''')
    # 5 financials
    fin = c["financials"]
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(fin["action_title"], 180))}</div><div class="rule"></div>
      <div class="cols" style="flex:0 0 auto">
        <div><div class="subhead">Выручка</div><img class="chart" src="{_img_uri(g("revenue_trend"))}" style="margin-top:12px"></div>
        <div><div class="subhead">Прибыль и рентабельность</div><img class="chart" src="{_img_uri(g("profit_margin"))}" style="margin-top:12px"></div>
      </div>
      <div style="margin-top:18px"><div class="subhead">Комментарий</div>
        <div class="body" style="margin-top:10px">{_e(_clip(fin.get("commentary", ""), 520))}</div></div>
      {_footer("ГИР БО (РСБУ); анализ", 5)}</div></div>''')
    # 6 catalysts
    hs = c.get("highlights_slide", {})
    cards = "".join(
        f'<div class="card"><h4>{_e(_clip(it.get("title"), 60))}</h4><p>{_e(_clip(it.get("text"), 230))}</p></div>'
        for it in (hs.get("catalysts") or [])[:4])
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(hs.get("action_title", "Точки роста"), 180))}</div><div class="rule"></div>
      <div class="cards">{cards}</div>{_footer("анализ", 6)}</div></div>''')
    # 7 valuation
    v = c["valuation"]
    method_just = (("Метод: " + _clip(v.get("method"), 90) + ". ") if v.get("method") else "") + _clip(
        v.get("justification", ""), 300)
    S.append(f'''<div class="slide"><div class="pad">
      <div class="kicker">{_e(_clip(c["company_name"], 90))}</div>
      <div class="atitle">{_e(_clip(v["action_title"], 180))}</div><div class="rule"></div>
      <div class="cols6040">
        <div><div class="subhead">Диапазон оценки (football field)</div>
          <img class="chart" src="{_img_uri(g("football_field"))}" style="margin-top:12px">
          <div class="body" style="font-size:12px;margin-top:10px">{_e(method_just)}</div></div>
        <div><div class="subhead">Что увеличит оценку</div>
          <ul style="margin-top:12px">{_bullets([_clip(x, 110) for x in (v.get("upside_drivers") or [])[:5]])}</ul>
          <div class="disc">{_e(_clip(v.get("disclaimer", ""), 400))}</div></div>
      </div>{_footer("ФНС / ГИР БО; анализ", 7)}</div></div>''')
    # 8 contact
    ct = c["contacts"]
    S.append(f'''<div class="cover"><div class="veil" style="background:{THEME["primary"]}"></div>
      <div class="ct"><div class="accentbar"></div>
        <h1>{_e(_clip(ct.get("headline", "Следующий шаг"), 80))}</h1>
        <div class="cta" style="color:{THEME["accent"]};font-size:18px;line-height:1.5;margin-top:18px;max-width:980px">{_e(_clip(ct.get("cta", ""), 360))}</div>
        <div class="body" style="color:#fff;margin-top:20px">{_e(_clip(ct.get("advisor", ""), 160))}</div>
        <div class="body" style="color:{THEME["muted"]};font-size:11px;margin-top:14px">{_e(_clip(ct.get("confidentiality", ""), 200))}</div>
      </div></div>''')
    return "".join(S)


def render(content: dict, charts: dict, images: dict, out_pdf: str | None = None) -> str:
    if not out_pdf:
        out_pdf = f"{slugify(content.get('company_name'))}.pdf"

    doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{_slides(content, charts, images)}</body></html>"
    htmlfile = Path(tempfile.gettempdir()) / "teaser_render.html"
    htmlfile.write_text(doc, "utf-8")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        pg.goto("file://" + str(htmlfile))
        pg.wait_for_timeout(400)
        pg.pdf(path=out_pdf, width="1280px", height="720px", print_background=True,
               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        b.close()
    return out_pdf
