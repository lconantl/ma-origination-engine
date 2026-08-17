# -*- coding: utf-8 -*-
"""
claude_client.py — тонкий клиент к LLM через Polza AI (OpenAI-совместимый API).

Используется для автогенерации контента/оценки тизера (generate_teaser.py).
Ключи — из .env: POLZA_API_KEY, POLZA_BASE_URL, POLZA_MODEL.
Модель на Polza может быть thinking-моделью (reasoning-блоки в тексте) — мы
устойчиво вырезаем JSON из ответа. Без стрима.
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
TIMEOUT = float(os.environ.get("POLZA_TIMEOUT", "320"))


class ClaudeError(RuntimeError):
    pass


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
            elif isinstance(content, list):   # на случай блочного content (vision-ответы)
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        out.append(b["text"])
    return "\n".join(out).strip()


def _messages(system: str, user_content) -> list:
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _post(body: dict) -> str:
    if not TOKEN:
        raise ClaudeError("POLZA_API_KEY не задан (.env)")
    url = f"{BASE_URL}/chat/completions"
    try:
        with httpx.Client(timeout=TIMEOUT) as cli:
            r = cli.post(url, headers=_headers(), json=body)
    except httpx.HTTPError as e:
        raise ClaudeError(f"сеть/Polza: {e}") from e
    if r.status_code >= 400:
        raise ClaudeError(f"HTTP {r.status_code}: {r.text[:400]}")
    try:
        data = r.json()
    except Exception:
        return r.text
    text = _extract_text(data)
    if not text:
        raise ClaudeError(f"пустой ответ: {json.dumps(data)[:400]}")
    return text


def complete(system: str, user: str, max_tokens: int = 8000,
             temperature: float = 0.2, model: str | None = None) -> str:
    """Один вызов модели → текст ответа (без стрима)."""
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
                    out.append(s[start:idx + 1]); start = None
    return out


def extract_json(text: str):
    """Достать валидный JSON-объект из ответа (снимает ```json и reasoning-обёртки)."""
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
    raise ClaudeError(f"не удалось распарсить JSON из ответа: {text[:300]}")


def complete_json(system: str, user: str, max_tokens: int = 8000,
                  temperature: float = 0.2, retries: int = 1) -> dict:
    """Вызов модели с гарантией валидного JSON (ретрай с напоминанием про формат)."""
    last = None
    for attempt in range(retries + 1):
        u = user if attempt == 0 else (
            user + "\n\nВНИМАНИЕ: верни ТОЛЬКО валидный JSON-объект без markdown-обёрток "
            "и без пояснений. Предыдущий ответ не распарсился.")
        try:
            text = complete(system, u, max_tokens=max_tokens, temperature=temperature)
            return extract_json(text)
        except ClaudeError as e:   # таймаут/сеть/парсинг — пробуем ещё раз
            last = e
    raise last


def ping() -> str:
    """Проба связи: модель должна вернуть слово PONG."""
    return complete("Ты эхо-сервис.", "Ответь одним словом: PONG", max_tokens=64)


if __name__ == "__main__":
    print("BASE_URL:", BASE_URL, "| MODEL:", MODEL, "| token:", (TOKEN[:6] + "…") if TOKEN else "—")
    try:
        print("PING →", ping())
    except Exception as e:
        print("PING FAILED:", e)
