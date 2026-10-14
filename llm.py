"""Каскад бесплатных моделей: OpenRouter -> AnyModel (как в message_checker.py бота)."""
import json
import re

from openai import OpenAI

import config

SYSTEM = "Ты — аналитик алгоритмической торговли. Отвечай кратко и по делу."


def _providers():
    return [("OpenRouter", "https://openrouter.ai/api/v1", "openrouter/free", config.OPENROUTER_API_KEY),
            ("AnyModel", "https://anymodel.org/v1", "am/free", config.ANYMODEL_API_KEY)]


def ask(prompt, system=SYSTEM):
    """-> (провайдер, текст) или (None, None), если никто не ответил."""
    for name, url, model, key in _providers():
        if not key:
            continue
        try:
            r = OpenAI(base_url=url, api_key=key, timeout=30, max_retries=0).chat.completions.create(
                model=model, temperature=0.0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            return name, r.choices[0].message.content
        except Exception as e:
            print(f"[WARN] {name} недоступен: {e}")
    return None, None


def ask_json(prompt):
    _, text = ask(prompt + "\nВерни ответ СТРОГО в формате JSON без markdown.")
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None
