"""Каскад бесплатных моделей: AnyModel -> Groq (все активные чат-модели по порядку предпочтения).
Логика Groq и разбор JSON взяты из message_checker.py бота."""
import json
import re
import time

from openai import OpenAI

import config

SYSTEM = "Ты — аналитик алгоритмической торговли. Отвечай кратко и по делу."

# Groq: не чат-модели исключаем; порядок попыток — от более сильных моделей к слабым
GROQ_SKIP = ("whisper", "guard", "tts", "orpheus", "compound")
GROQ_PREFERRED = ("llama-3.3-70b", "gpt-oss-120b", "qwen", "gpt-oss-20b", "llama-3.1-8b")
_groq_cache = {"ts": 0.0, "models": []}


def _groq_models(client):
    """Список чат-моделей Groq (кэш на час)."""
    if _groq_cache["models"] and time.time() - _groq_cache["ts"] < 3600:
        return _groq_cache["models"]
    try:
        ids = [m.id for m in client.models.list().data
               if getattr(m, "active", True) and not any(x in m.id for x in GROQ_SKIP)]
    except Exception:
        if _groq_cache["models"]:
            return _groq_cache["models"]
        raise
    ids.sort(key=lambda i: next((n for n, p in enumerate(GROQ_PREFERRED) if p in i), len(GROQ_PREFERRED)))
    _groq_cache.update(ts=time.time(), models=ids)
    return ids


def _strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)  # рассуждения reasoning-моделей


def extract_json(text):
    m = re.search(r"\{.*\}", _strip_think(text), re.DOTALL)
    if not m:
        raise ValueError(f"в ответе нет JSON: {text[:150]!r}")
    return json.loads(m.group(0))


def _err(e):
    s = f"{type(e).__name__}: {e}"
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            s += f" | HTTP {resp.status_code} {resp.text[:200]}"
        except Exception:
            pass
    return s[:500]


def ask(prompt, system=SYSTEM, fast=False, parse=None, log=None):
    """-> (провайдер, результат) или (None, None). parse - функция разбора текста; если бросает исключение,
    ответ считается неудачным и берётся следующая модель. fast=True - короткие таймауты для торгового пути.
    log - список, куда складываются строки о каждой попытке (для самотеста)."""
    def note(msg):
        print(msg)
        if log is not None:
            log.append(msg)

    timeout, retries = (20, 0) if fast else (60, 1)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def attempt(client, model, name):
        t0 = time.time()
        text = client.chat.completions.create(model=model, messages=msgs, temperature=0.0).choices[0].message.content
        if not text:
            raise ValueError("пустой ответ")
        res = parse(text) if parse else _strip_think(text)
        note(f"[OK] {name} ({model}) за {time.time() - t0:.1f}с")
        return res

    # 1. AnyModel
    try:
        client = OpenAI(base_url="https://anymodel.org/v1", api_key=config.ANYMODEL_API_KEY, timeout=timeout, max_retries=retries)
        return "AnyModel", attempt(client, "am/free", "AnyModel")
    except Exception as e:
        note(f"[WARN] AnyModel (am/free) не сработал: {_err(e)}")

    # 2. Groq
    if config.GROQ_API_KEY:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=config.GROQ_API_KEY, timeout=timeout, max_retries=0)
        try:
            models = _groq_models(client)
        except Exception as e:
            note(f"[WARN] Не удалось получить список моделей Groq: {_err(e)}")
            models = []
        for model in models[:3] if fast else models:
            try:
                return "Groq", attempt(client, model, "Groq")
            except Exception as e:
                note(f"[WARN] Groq ({model}) не сработал: {_err(e)}")
                time.sleep(0.5)
    else:
        note("[INFO] GROQ_API_KEY не задан - Groq пропущен")
    return None, None


def ask_json(prompt, fast=False, log=None):
    _, data = ask(prompt + "\nВерни ответ СТРОГО в формате JSON без markdown.", fast=fast, parse=extract_json, log=log)
    return data
