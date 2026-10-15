"""Диагностика: хранилище (S3/локально), ИИ, внешние API. Используется страницей управления и `python selftest.py live`.
Секреты не выводятся: ключ показывается только началом и длиной."""
import json
import os
import re
import time

import config
import llm
import storage

CLUSTER_REGION = {"000": "us-west-000", "001": "us-west-001", "002": "us-west-002",
                  "003": "eu-central-003", "004": "us-west-004", "005": "us-east-005"}


def _mask(v):
    return f"{v[:6]}… (длина {len(v)})" if v else "(пусто)"


def storage_summary():
    if not config.USE_S3:
        return f"Хранилище: локально, папка {config.DATA_DIR}"
    return (f"Хранилище: S3 | endpoint={config.S3_ENDPOINT_URL or '(пусто)'} | bucket={config.S3_BUCKET or '(пусто)'} | "
            f"prefix={config.S3_PREFIX} | keyID={_mask(config.S3_ACCESS_KEY)} | secret: длина {len(config.S3_SECRET_KEY)}")


def explain(e):
    """Подробное описание ошибки + подсказки по частым причинам (Backblaze B2)."""
    lines = [f"{type(e).__name__}: {e}"]
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        err, meta = resp.get("Error", {}), resp.get("ResponseMetadata", {})
        lines.append(f"код={err.get('Code')} http={meta.get('HTTPStatusCode')} request_id={meta.get('RequestId')}")
    lines.append(storage_summary())
    if not config.USE_S3:
        return "\n".join(lines)
    ep, kid, sec = config.S3_ENDPOINT_URL, config.S3_ACCESS_KEY, config.S3_SECRET_KEY
    code = (resp or {}).get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    hints = []
    m = re.search(r"-(\d{3})\.backblazeb2", ep)
    ep_c, key_c = (m.group(1) if m else None), (kid[:3] if re.match(r"^\d{3}", kid) else None)
    if ep_c and key_c and ep_c != key_c:
        hints.append(f"Ключ создан в кластере {key_c}, а endpoint указывает на {ep_c}. Для такого ключа endpoint обычно "
                     f"https://s3.{CLUSTER_REGION.get(key_c, '<регион>')}.backblazeb2.com - сверьте с полем Endpoint в карточке бакета.")
    if ep and not ep.startswith("https://"):
        hints.append("S3_ENDPOINT_URL должен начинаться с https://")
    if sec and re.match(r"^\d{3}", sec) and len(sec) <= 26:
        hints.append("S3_SECRET_KEY похож на keyID: возможно, ключи перепутаны местами (keyID ~25 символов, applicationKey ~31 и начинается с K).")
    if code == "InvalidAccessKeyId":
        hints.append("Ключ не найден на этом endpoint: проверьте keyID, не удалён ли ключ, нет ли лишних символов при вставке в Render.")
    elif code in ("SignatureDoesNotMatch",):
        hints.append("keyID найден, но секрет не подходит: проверьте S3_SECRET_KEY (applicationKey показывается только при создании).")
    elif code in ("AccessDenied", "403"):
        hints.append("Ключ не имеет доступа к этому бакету или к операции (проверьте Allow access to bucket и права Read and Write).")
    elif code == "NoSuchBucket":
        hints.append("Бакет с таким именем не найден: проверьте S3_BUCKET.")
    return "\n".join(lines + ["Подсказки: " + h for h in hints])


def _step(out, ok, text):
    out.append(("[OK]   " if ok else "[FAIL] ") + text)
    return ok


def check_storage(out):
    out.append(storage_summary())
    try:
        st = storage.get_storage()
    except Exception as e:
        return _step(out, False, "создание клиента хранилища:\n" + explain(e))
    name, payload = "selftest.json", {"ts": time.time()}
    try:
        st.save(name, payload)
        back = st.load(name)
        if back != payload:
            return _step(out, False, f"запись/чтение: прочитано {back!r}, ожидалось {payload!r}")
        _step(out, True, f"запись и чтение ({type(st).__name__})")
    except Exception as e:
        return _step(out, False, "запись/чтение:\n" + explain(e))
    try:
        if isinstance(st, storage.S3Storage):
            st.s3.delete_object(Bucket=config.S3_BUCKET, Key=config.S3_PREFIX + name)
        else:
            os.remove(os.path.join(config.DATA_DIR, name))
        _step(out, True, "удаление тестового файла")
    except Exception as e:
        _step(out, True, f"тестовый файл не удалён (не критично, нет права на удаление?): {type(e).__name__}: {e}")
    return True


def check_ai(out):
    log = []
    provider, data = llm.ask('Ответь JSON {"ok": true}', fast=False, parse=llm.extract_json, log=log)
    out.extend("       " + x for x in log)
    return _step(out, bool(provider), f"ИИ ответил через {provider}: {json.dumps(data, ensure_ascii=False)}" if provider
                 else "ни AnyModel, ни Groq не ответили (подробности выше)")


def check_network(out):
    import hl
    import webcheck
    ok = True
    for label, fn in (("Hyperliquid allMids", lambda: f"{len(hl.mids())} монет"),
                      ("Hyperliquid лидерборд", lambda: f"{len(hl.leaderboard())} строк"),
                      ("Google News RSS", lambda: f"{len(webcheck.headlines('bitcoin'))} заголовков")):
        try:
            ok &= _step(out, True, f"{label}: {fn()}")
        except Exception as e:
            ok &= _step(out, False, f"{label}: {type(e).__name__}: {e}")
    return ok


def run_all():
    out = [f"Самотест {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} | Render={config.IS_RENDER}"]
    res = [check_storage(out), check_ai(out), check_network(out)]
    out.append("ИТОГ: " + ("всё работает" if all(res) else "есть ошибки (см. [FAIL])"))
    text = "\n".join(out)
    print(text)
    return text
