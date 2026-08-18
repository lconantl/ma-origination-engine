# -*- coding: utf-8 -*-
"""
claude_client.py — тонкий клиент к LLM через Polza AI (OpenAI-совместимый API).

Используется для автогенерации контента/оценки тизера (generate_teaser.py).
Ключи — из .env: POLZA_API_KEY, POLZA_BASE_URL, POLZA_MODEL.

Модель на Polza может быть thinking-моделью (reasoning-блоки в тексте) — мы
устойчиво вырезаем JSON из ответа.

ЗАПРОСЫ ИДУТ СТРИМОМ. Это принципиально: при non-streaming вызове клиент не
получает ни одного байта, пока модель не закончит генерацию целиком, и любой
промежуточный прокси/CDN рвёт «молчащее» соединение по своему таймауту (60-120 с)
независимо от наших настроек. В стриме read-таймаут считается МЕЖДУ чанками,
поэтому модель может думать сколько угодно — соединение остаётся живым.
Если провайдер не поддерживает stream для модели — автоматический фолбэк
на обычный POST (см. _post / STREAM_SUPPORTED).
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

import httpx


# --- .env (без внешних зависимостей) ---
# ВАЖНО: .env ПЕРЕОПРЕДЕЛЯЕТ окружение.
def _load_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


_load_env()

BASE_URL = os.environ.get("POLZA_BASE_URL", "https://api.polza.ai/api/v1").rstrip("/")
TOKEN = os.environ.get("POLZA_API_KEY", "")
MODEL = os.environ.get("POLZA_MODEL", "openai/gpt-5")

# read — пауза МЕЖДУ чанками стрима, а не лимит на весь ответ.
# 180 с с запасом покрывает reasoning-паузу перед первым токеном.
TIMEOUT = httpx.Timeout(
    connect=float(os.environ.get("POLZA_CONNECT_TIMEOUT", "10")),
    read=float(os.environ.get("POLZA_READ_TIMEOUT", "180")),
    write=60.0,
    pool=60.0,
)
# Полный лимит на non-stream фолбэк (там ждём весь ответ разом).
TIMEOUT_NOSTREAM = httpx.Timeout(
    connect=10.0,
    read=float(os.environ.get("POLZA_TIMEOUT", "320")),
    write=60.0,
    pool=60.0,
)

# Переключается в False, если провайдер отверг stream — чтобы не долбиться повторно.
STREAM_SUPPORTED = os.environ.get("POLZA_STREAM", "1") not in ("0", "false", "no")


class ClaudeError(RuntimeError):
    pass


class ClaudeTimeout(ClaudeError):
    """Сетевой таймаут. Ретраить тем же промптом бессмысленно — только дольше ждать."""


def _headers():
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {TOKEN}",
    }


def _extract_text(data: dict) -> str:
    """Собрать текст ответа /chat/completions (OpenAI-совместимый формат Polza)."""
    out = []
    if isinstance(data.get("choices"), list):
        for ch in data["choices"]:
            msg = (ch or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content:
                out.append(content)
            elif isinstance(content, list):  # на случай блочного content (vision-ответы)
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        out.append(b["text"])
            # фолбэк для reasoning-моделей: некоторые провайдеры кладут весь вывод сюда
            if not out:
                rc = msg.get("reasoning_content") or msg.get("reasoning")
                if isinstance(rc, str) and rc:
                    out.append(rc)
    return "\n".join(out).strip()


def _messages(system: str, user_content) -> list:
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _sse_payload(line: str) -> str | None:
    """Из строки SSE достать полезную нагрузку. Терпим 'data: {}' и 'data:{}'."""
    if not line:
        return None
    if line.startswith("data:"):
        return line[5:].strip()
    return None


def _post_stream(url: str, body: dict) -> str:
    """POST со стримом. Возвращает склеенный текст дельт."""
    body = {**body, "stream": True}
    chunks: list[str] = []
    reasoning: list[str] = []
    try:
        with httpx.Client(timeout=TIMEOUT) as cli:
            with cli.stream("POST", url, headers=_headers(), json=body) as r:
                if r.status_code >= 400:
                    r.read()  # без этого .text у стрим-ответа пустой
                    raise ClaudeError(f"HTTP {r.status_code}: {r.text[:400]}")
                for line in r.iter_lines():
                    payload = _sse_payload(line)
                    if payload is None or payload == "":
                        continue
                    if payload == "[DONE]":
                        break
                    try:
                        ev = json.loads(payload)
                    except Exception:
                        continue  # keep-alive комментарии и мусор игнорируем
                    if isinstance(ev.get("error"), dict):
                        msg = ev["error"].get("message") or json.dumps(ev["error"])[:300]
                        raise ClaudeError(f"ошибка провайдера в стриме: {msg}")
                    for ch in ev.get("choices") or []:
                        delta = (ch or {}).get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str):
                            chunks.append(piece)
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(rc, str):
                            reasoning.append(rc)
    except httpx.TimeoutException as e:
        got = len("".join(chunks))
        raise ClaudeTimeout(
            f"таймаут Polza (пауза между чанками > {TIMEOUT.read:.0f}с, получено {got} симв.): {e}"
        ) from e
    except httpx.HTTPError as e:
        raise ClaudeError(f"сеть/Polza: {e}") from e

    text = "".join(chunks).strip()
    if not text:
        text = "".join(reasoning).strip()  # модель отдала всё в reasoning-канал
    return text


def _post_plain(url: str, body: dict) -> str:
    """Обычный POST без стрима — фолбэк, если провайдер не принял stream."""
    try:
        with httpx.Client(timeout=TIMEOUT_NOSTREAM) as cli:
            r = cli.post(url, headers=_headers(), json=body)
    except httpx.TimeoutException as e:
        raise ClaudeTimeout(f"таймаут Polza (non-stream): {e}") from e
    except httpx.HTTPError as e:
        raise ClaudeError(f"сеть/Polza: {e}") from e
    if r.status_code >= 400:
        raise ClaudeError(f"HTTP {r.status_code}: {r.text[:400]}")
    try:
        data = r.json()
    except Exception:
        return r.text
    return _extract_text(data)


def _post(body: dict) -> str:
    global STREAM_SUPPORTED
    if not TOKEN:
        raise ClaudeError("POLZA_API_KEY не задан (.env)")
    url = f"{BASE_URL}/chat/completions"

    if STREAM_SUPPORTED:
        try:
            text = _post_stream(url, body)
        except ClaudeError as e:
            # 400/404/422 на stream => модель/шлюз его не умеет: разово падаем на plain
            if isinstance(e, ClaudeTimeout) or not re.search(r"HTTP (400|404|405|422)", str(e)):
                raise
            STREAM_SUPPORTED = False
            text = _post_plain(url, body)
    else:
        text = _post_plain(url, body)

    if not text:
        raise ClaudeError("пустой ответ модели")
    return text


def complete(system: str, user: str, max_tokens: int = 8000,
             temperature: float = 0.2, model: str | None = None) -> str:
    """Один вызов модели → текст ответа."""
    body = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": _messages(system, user),
    }
    return _post(body)


def complete_blocks(system: str, content: list, max_tokens: int = 1500,
                    temperature: float = 0.0, model: str | None = None) -> str:
    """Вызов с произвольным списком блоков user-контента (текст + изображения)."""
    body = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": _messages(system, content),
    }
    return _post(body)


def image_block(path: str, media_type: str = "image/jpeg") -> dict:
    """Блок изображения (data-URI) для complete_blocks (OpenAI vision-формат)."""
    import base64
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}


def _balanced_objects(s: str) -> list:
    """Все сбалансированные {...}-подстроки верхнего уровня (учёт строк JSON в кавычках).
    Устойчиво к reasoning-прозе со «стай»-скобками до/после реального JSON."""
    out, stack, start, in_str, esc = [], 0, None, False, False
    for idx, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if stack == 0:
                start = idx
            stack += 1
        elif ch == "}":
            if stack > 0:
                stack -= 1
                if stack == 0 and start is not None:
                    out.append(s[start:idx + 1])
                    start = None
    return out


def extract_json(text: str):
    """Достать валидный JSON-объект из ответа (снимает ```json и reasoning-обёртки).

    ВАЖНО: не распаковывать {"content": {...}} как обёртку провайдера — в этом
    проекте "content" часто и есть ЗНАЧИМЫЙ ключ верхнего уровня нашей собственной
    схемы (см. схему в generate_teaser._user_prompt: {"content":..., "chart_data":...}).
    Распаковка ломала бы такие ответы, отбрасывая соседние ключи."""
    if not text:
        raise ClaudeError("пустой текст для JSON")
    candidates = []
    # 1) приоритет — содержимое fenced-блоков ```json ... ```
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.S):
        candidates.extend(_balanced_objects(m.group(1)))
    # 2) все сбалансированные объекты во всём тексте (длинные раньше — это и есть контент)
    candidates.extend(_balanced_objects(text))
    seen = set()
    for c in sorted(candidates, key=len, reverse=True):
        if c in seen:
            continue
        seen.add(c)
        try:
            return json.loads(c)
        except Exception:
            continue
    raise ClaudeError(f"не удалось распарсить JSON из ответа: {text[:500]}")


def complete_json(system: str, user: str, max_tokens: int = 8000,
                  temperature: float = 0.2, retries: int = 1) -> dict:
    """Вызов модели с гарантией валидного JSON (ретрай с напоминанием про формат).

    Ретраим ТОЛЬКО ошибки парсинга. На таймауте выходим сразу: повторный запрос
    уйдёт с более длинным промптом и будет только медленнее — это удваивало
    время до падения."""
    last = None
    for attempt in range(retries + 1):
        u = user if attempt == 0 else (
                user + "\n\nВНИМАНИЕ: верни ТОЛЬКО валидный JSON-объект без markdown-обёрток "
                       "и без пояснений. Предыдущий ответ не распарсился.")
        try:
            text = complete(system, u, max_tokens=max_tokens, temperature=temperature)
            return extract_json(text)
        except ClaudeTimeout:
            raise
        except ClaudeError as e:  # парсинг/пустой ответ — пробуем ещё раз
            last = e
    raise last


def ping() -> str:
    """Проба связи: модель должна вернуть слово PONG."""
    return complete("Ты эхо-сервис.", "Ответь одним словом: PONG", max_tokens=64)


if __name__ == "__main__":
    print("BASE_URL:", BASE_URL, "| MODEL:", MODEL, "| token:", (TOKEN[:6] + "…") if TOKEN else "—")
    print("stream:", STREAM_SUPPORTED, "| read timeout:", TIMEOUT.read)
    try:
        print("PING →", ping())
        print("stream после пинга:", STREAM_SUPPORTED)
    except Exception as e:
        print("PING FAILED:", type(e).__name__, e)
