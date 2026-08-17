# -*- coding: utf-8 -*-
"""
chart_builder.py — графики для тизера. Агент сам выбирает УМЕСТНЫЕ под актив графики
(только там, где есть данные), затем рендерит из ТОЧНЫХ цифр в PNG.

Логика «агент решает»: select_charts(...) смотрит, какие данные доступны, и возвращает
рекомендации (тип графика + обоснование). build_charts(...) рендерит рекомендованные.
Числа — из data_fetcher/market_research/comparables, не выдуманные.

Стиль вынесен в CHART_STYLE -> легко перекрасить под бренд позже.
Зависимости: matplotlib.
"""
from __future__ import annotations
from pathlib import Path

import logging
import matplotlib
matplotlib.use("Agg")
logging.getLogger("matplotlib").setLevel(logging.WARNING)
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

CHART_STYLE = {  # синхронно с render_html.THEME (Big3-палитра)
    "primary": "#04244A", "accent": "#00A9CE", "neg": "#C0392B",
    "grid": "#E3E8EE", "muted": "#8A94A6", "text": "#1A1A1A", "bg": "#FFFFFF",
    "context": "#C7D0DA",
    # единый шрифт со слайдами (render_html.THEME). PT Sans: кириллица + знак ₽ (U+20BD).
    # Fallback'ы тоже содержат ₽ (Helvetica Neue, DejaVu Sans) — рендер не ломается.
    "font": ["PT Sans", "Helvetica Neue", "DejaVu Sans"],
}
plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": CHART_STYLE["font"],
                     "axes.edgecolor": CHART_STYLE["grid"],
                     "text.color": CHART_STYLE["text"], "axes.labelcolor": CHART_STYLE["text"],
                     "xtick.color": CHART_STYLE["text"], "ytick.color": CHART_STYLE["text"],
                     "svg.fonttype": "none"})


def _thsd_to_mln(x):
    return None if x is None else x / 1e3


def _cagr(first, last, n):
    if not first or not last or first <= 0 or n <= 0:
        return None
    return ((last / first) ** (1 / n) - 1) * 100


def _years_sorted(years: dict, n=5):
    ks = sorted((k for k in years if str(k).isdigit()))[-n:]
    return ks


def _save(fig, path):
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=CHART_STYLE["bg"])
    plt.close(fig)
    return path


def _clean(ax, hide_y=True):
    """Consulting-grade: убрать ось Y, рамки, сетку (прямые метки несут значения)."""
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(CHART_STYLE["grid"])
    if hide_y:
        ax.yaxis.set_visible(False)
    ax.tick_params(axis="x", length=0, labelsize=10, colors=CHART_STYLE["text"])
    ax.margins(y=0.18)


def _fmt(v):
    if v is None:
        return ""
    a = abs(v)
    if 0 < a < 1:
        return f"{v:.2f}".replace(".", ",")
    if 0 < a < 10:
        return f"{v:.1f}".replace(".", ",")
    return f"{v:,.0f}".replace(",", " ")


# ---------- отдельные графики (рендер из точных данных) ----------
def chart_revenue_trend(years: dict, path: str, unit="млн ₽"):
    ks = _years_sorted(years)
    vals = [_thsd_to_mln(years[k].get("revenue")) for k in ks]
    if sum(1 for v in vals if v) < 2:
        return None
    fig, ax = plt.subplots(figsize=(6, 3.0))
    # единый цвет (McKinsey): без акцента на годе спада; пик — слегка выделен
    peak = max(range(len(vals)), key=lambda i: vals[i] if vals[i] else -1)
    colors = [CHART_STYLE["accent"] if i == peak else CHART_STYLE["primary"] for i in range(len(vals))]
    ax.bar(ks, vals, color=colors, width=0.55)
    for x, v in zip(ks, vals):
        if v:
            ax.text(x, v, _fmt(v), ha="center", va="bottom", fontsize=9.5,
                    color=CHART_STYLE["text"], weight="bold")
    # CAGR считаем по фазе роста (до пика), чтобы не противоречить спаду
    g = _cagr(next((v for v in vals if v), None), vals[peak], peak) if peak > 0 else None
    yrs = f" {ks[0]}–{ks[peak]}" if g else ""
    title = "Выручка, " + unit + (f"   ·   CAGR +{g:.0f}%{yrs}" if g else "")
    ax.set_title(title, fontsize=11, color=CHART_STYLE["primary"], loc="left", weight="bold", pad=12)
    _clean(ax)
    return _save(fig, path)


def chart_profit_margin(years: dict, path: str):
    ks = _years_sorted(years)
    ebit = [_thsd_to_mln(years[k].get("ebit")) for k in ks]
    rev = [_thsd_to_mln(years[k].get("revenue")) for k in ks]
    if sum(1 for v in ebit if v is not None) < 2:
        return None
    margin = [(e / r * 100 if (e is not None and r) else None) for e, r in zip(ebit, rev)]
    fig, ax = plt.subplots(figsize=(6, 3.0))
    bars = ax.bar(ks, ebit, color=CHART_STYLE["context"], width=0.62, zorder=1)

    evals = [e for e in ebit if e is not None]
    emax = max(evals); emin = min(evals)
    halo = dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.9)
    # МЕТКИ EBIT — над столбцом (для отриц. — под): bar-зона = нижняя половина графика,
    # линия маржи вынесена ВЫШЕ → метки бара и метки маржи не пересекаются.
    for x, e in zip(ks, ebit):
        if e is None:
            continue
        va, dy = ("bottom", 4) if e >= 0 else ("top", -4)
        ax.annotate(_fmt(e), (x, e), xytext=(0, dy), textcoords="offset points",
                    ha="center", va=va, fontsize=9, color=CHART_STYLE["muted"])

    ax2 = ax.twinx()
    line, = ax2.plot(ks, margin, color=CHART_STYLE["accent"], marker="o", linewidth=2.4,
                     markersize=6, zorder=4)
    # МЕТКИ МАРЖИ — над маркером, с белым ореолом
    for x, m in zip(ks, margin):
        if m is not None:
            ax2.annotate(f"{m:.0f}%", (x, m), xytext=(0, 9), textcoords="offset points",
                         ha="center", va="bottom", fontsize=9.5, weight="bold",
                         color=CHART_STYLE["primary"], bbox=halo, zorder=5)
    # размещаем линию маржи в ВЕРХНЕЙ полосе графика (доля высоты 0.60–0.90) —
    # выше любых столбцов, чтобы маркеры/линия не накрывали столбцы и их метки.
    mvals = [m for m in margin if m is not None]
    mlo, mhi = min(mvals), max(mvals)
    f_lo, f_hi = 0.60, 0.90
    span = (mhi - mlo) or max(1.0, abs(mhi) * 0.5)
    range2 = span / (f_hi - f_lo)
    b2lo = mlo - f_lo * range2
    ax2.set_ylim(bottom=b2lo, top=b2lo + range2)
    ax2.yaxis.set_visible(False)                 # каждая точка подписана → шкала не нужна
    for s in ("top", "left", "right"):
        ax2.spines[s].set_visible(False)
    # ЧИСТАЯ ЛЕГЕНДА НАД графиком (вне области данных)
    ax.legend([bars, line], ["EBIT, млн ₽", "Маржа EBIT, %"], loc="lower left",
              bbox_to_anchor=(0, 1.02), ncol=2, frameon=False, fontsize=9.5,
              handlelength=1.4, columnspacing=1.6, labelcolor=CHART_STYLE["primary"])
    _clean(ax)
    # столбцы в нижней части (верх отдан линии маржи) — устойчиво к знаку EBIT
    # (раньше при emax<0 пределы переворачивались). Для положительных данных
    # эквивалентно прежнему (0..emax*1.9).
    lo = min(0, emin); hi = max(0, emax); pad = (hi - lo) or 1.0
    ax.set_ylim(bottom=lo - 0.15 * pad, top=hi + 0.9 * pad)
    return _save(fig, path)


def chart_football_field(methods: list, base: float, path: str, unit="млн ₽"):
    """Диапазоны оценки по методам. methods: [{'name','low','high','base'(опц)}], base=итог."""
    methods = [m for m in (methods or []) if m.get("low") is not None and m.get("high") is not None]
    if not methods:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 3.0))
    names = [m["name"] for m in methods]
    y = range(len(methods))
    lo_all = min(m["low"] for m in methods); hi_all = max(m["high"] for m in methods)
    # БАЗА входит в расчёт границ — иначе линия/плашка базы вне диапазона исчезали
    if base:
        lo_all = min(lo_all, base); hi_all = max(hi_all, base)
    span = (hi_all - lo_all) or max(1.0, abs(hi_all) * 0.1)  # защита от нулевого размаха
    pad = span * 0.035  # отступ меток ОТ полосы
    for i, m in enumerate(methods):
        if m["high"] - m["low"] < span * 0.01:   # вырожденный диапазон → точка + одна метка
            ax.plot([m["low"]], [i], marker="o", markersize=11, color=CHART_STYLE["primary"], zorder=2)
            ax.text(m["high"] + pad, i, _fmt(m["low"]), va="center", ha="left", fontsize=9,
                    color=CHART_STYLE["text"])
            continue
        ax.plot([m["low"], m["high"]], [i, i], color=CHART_STYLE["primary"], linewidth=11,
                solid_capstyle="round", zorder=2)
        # метки СНАРУЖИ полосы (не налезают)
        ax.text(m["low"] - pad, i, _fmt(m["low"]), va="center", ha="right", fontsize=9,
                color=CHART_STYLE["text"])
        ax.text(m["high"] + pad, i, _fmt(m["high"]), va="center", ha="left", fontsize=9,
                color=CHART_STYLE["text"])
    if base:
        # линия базы — под текстом по zorder; метка вынесена ВЫШЕ верхней полосы
        ax.axvline(base, color=CHART_STYLE["accent"], linewidth=2.2, linestyle=(0, (5, 3)),
                   zorder=3, ymax=0.86)
        ax.annotate(f"База: {_fmt(base)}", xy=(base, len(methods) - 1), xytext=(0, 34),
                    textcoords="offset points", ha="center", va="bottom",
                    color="#fff", fontsize=10, weight="bold", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.4", fc=CHART_STYLE["accent"], ec="none"))
    ax.set_yticks(list(y)); ax.set_yticklabels(names, fontsize=10.5, color=CHART_STYLE["text"])
    ax.set_title(f"Диапазон оценки EV, {unit}", fontsize=11, color=CHART_STYLE["primary"],
                 loc="left", weight="bold", pad=22)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    # единая шкала X внизу — адаптивное округление (на малых значениях не схлопывается в 0)
    import numpy as np
    nd = 0 if span >= 100 else (-1 if span >= 20 else None)
    ticks = np.linspace(lo_all, hi_all, 4)
    if nd is not None:
        ticks = np.round(ticks, nd)
    ax.set_xticks(ticks)
    ax.set_xticklabels([_fmt(t) for t in ticks], fontsize=8.5, color=CHART_STYLE["muted"])
    ax.spines["bottom"].set_color(CHART_STYLE["grid"])
    ax.tick_params(axis="x", length=3, color=CHART_STYLE["grid"])
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(lo_all - span * 0.18, hi_all + span * 0.18)
    ax.set_ylim(-0.7, len(methods) - 1 + 1.15)   # запас сверху под callout «База»
    return _save(fig, path)


def chart_market(market: dict, path: str):
    """market: {'years':[...], 'values':[...], 'unit':'млрд ₽', 'title':...}"""
    ys, vs = market.get("years"), market.get("values")
    if not ys or not vs or len(ys) != len(vs):
        return None
    fig, ax = plt.subplots(figsize=(6, 3.0))
    peak = max(range(len(vs)), key=lambda i: vs[i])
    colors = [CHART_STYLE["accent"] if i == peak else CHART_STYLE["primary"] for i in range(len(vs))]
    ax.bar(ys, vs, color=colors, width=0.55)
    for x, v in zip(ys, vs):
        ax.text(x, v, f"{v:g}", ha="center", va="bottom", fontsize=9.5,
                color=CHART_STYLE["text"], weight="bold")
    # пометить последний год сноской ПОД осью X (не поверх столбцов)
    last_note = market.get("last_note")
    if last_note:
        ax.annotate(last_note, xy=(1.0, -0.16), xycoords="axes fraction",
                    ha="right", va="top", fontsize=8, color=CHART_STYLE["muted"])
    g = _cagr(vs[0], vs[peak], peak) if peak > 0 else None
    import textwrap
    title = (market.get("title") or "Объём рынка") + f", {market.get('unit','')}" + \
            (f"   ·   +{g:.0f}%/год ({ys[0]}–{ys[peak]})" if g else "")
    title = "\n".join(textwrap.wrap(title, 52)) or title   # длинный заголовок не раздувает фигуру
    ax.set_title(title, fontsize=11, color=CHART_STYLE["primary"], loc="left", weight="bold", pad=12)
    _clean(ax)
    return _save(fig, path)


def chart_segments(segments: dict, path: str):
    """segments: {'Сегмент A': value, ...} — обычно из управленческих данных/сайта."""
    segments = {k: v for k, v in (segments or {}).items() if v}
    if len(segments) < 2:
        return None
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    colors = [CHART_STYLE["primary"], CHART_STYLE["accent"], "#5D8AA8", "#8FB339", "#B0707D"]
    dark = {CHART_STYLE["primary"], "#5D8AA8", "#B0707D"}   # тёмные сектора → белый текст %
    wedges, _txt, autotexts = ax.pie(
        segments.values(), labels=segments.keys(), autopct="%1.0f%%",
        colors=colors[:len(segments)], textprops={"fontsize": 9})
    for at, col in zip(autotexts, colors[:len(segments)]):
        at.set_color("#fff" if col in dark else CHART_STYLE["primary"])
        at.set_fontweight("bold")
    ax.set_title("Структура выручки по сегментам", fontsize=11,
                 color=CHART_STYLE["primary"], weight="bold")
    return _save(fig, path)


# ---------- агент решает, какие графики уместны ----------
def select_charts(financials: dict = None, market: dict = None,
                  comps: list = None, segments: dict = None) -> list[dict]:
    """Возвращает рекомендации: какие графики строить и почему (по наличию данных)."""
    rec = []
    years = (financials or {}).get("years") or {}
    has_rev = sum(1 for k in years if years[k].get("revenue")) >= 2
    has_ebit = sum(1 for k in years if years[k].get("ebit") is not None) >= 2
    if has_rev:
        rec.append({"type": "revenue_trend", "reason": "есть выручка за ≥2 года — динамика роста"})
    if has_ebit:
        rec.append({"type": "profit_margin", "reason": "есть EBIT — прибыль и маржа"})
    if market and market.get("values"):
        rec.append({"type": "market", "reason": "есть данные рынка — рост сегмента"})
    # секция «сравнимые сделки» убрана из тизера (оценка на EV/EBITDA + NAV)
    if segments and len([v for v in segments.values() if v]) >= 2:
        rec.append({"type": "segments", "reason": "есть разбивка выручки по сегментам"})
    return rec


_RENDERERS = {
    "revenue_trend": lambda d, p: chart_revenue_trend(d["financials"]["years"], p),
    "profit_margin": lambda d, p: chart_profit_margin(d["financials"]["years"], p),
    "market": lambda d, p: chart_market(d["market"], p),
    "segments": lambda d, p: chart_segments(d["segments"], p),
    "football_field": lambda d, p: chart_football_field(
        d["valuation"]["methods"], d["valuation"].get("base"), p,
        unit=d["valuation"].get("unit", "млн ₽")),
}


def build_charts(data: dict, outdir="charts") -> list[dict]:
    """data: {financials, market, comps, segments, valuation}. Рендерит уместные -> PNG."""
    Path(outdir).mkdir(parents=True, exist_ok=True)
    rec = select_charts(data.get("financials"), data.get("market"),
                        data.get("comps"), data.get("segments"))
    if (data.get("valuation") or {}).get("methods"):
        rec.append({"type": "football_field", "reason": "диапазон оценки по методам"})
    built = []
    for r in rec:
        path = str(Path(outdir) / f"{r['type']}.png")
        try:
            res = _RENDERERS[r["type"]](data, path)
        except Exception as e:
            res = None; r["error"] = str(e)
        if res:
            built.append({"type": r["type"], "path": res, "caption": r["reason"]})
    return built


if __name__ == "__main__":
    import data_fetcher as df, comparables as cmp
    inn = "7743195810"  # Кармин-Авто
    fin = df.get_financials(inn)
    comps = cmp.find_comparables("manufacturing", n=6)
    market = {"years": ["2022", "2023", "2024", "2025"], "values": [660, 700, 780, 750],
              "unit": "млрд ₽", "title": "Рынок грузовых автозапчастей РФ"}
    built = build_charts({"financials": fin, "market": market, "comps": comps}, outdir="charts_carmin")
    print("Построены графики:")
    for b in built:
        print(f"  ✅ {b['type']:16} -> {b['path']}  ({b['caption']})")
