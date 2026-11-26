"""Синхронизация SQLite БД с Backblaze B2 (S3) со сжатием gzip (для эфемерного диска Render).
Использует те же переменные окружения S3_*, что и insider-trade."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("bot.s3")


def is_render() -> bool:
    return bool(os.getenv("RENDER"))


def s3_enabled() -> bool:
    mode = os.getenv("USE_S3", "auto").strip().lower()
    want = is_render() if mode == "auto" else mode in ("1", "true", "yes")
    if not want:
        return False
    return all(
        os.getenv(k, "").strip()
        for k in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET")
    )


def _client():
    import boto3
    ep = os.getenv("S3_ENDPOINT_URL", "").strip()
    region = ep.split("s3.")[1].split(".")[0] if "s3." in ep else None
    return boto3.client(
        "s3",
        endpoint_url=ep,
        region_name=region,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY", "").strip(),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY", "").strip(),
    )


def _remote_key(remote_name: str) -> str:
    prefix = os.getenv("S3_PREFIX", "crypto-bot/").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{remote_name}.gz"


class S3SyncManager:
    """Управляет восстановлением БД при старте и периодическим сжатым бэкапом."""

    def __init__(self, db_path: str | Path, remote_name: str = "bot.db"):
        self.db_path = Path(db_path)
        self.remote_name = remote_name
        self.enabled = s3_enabled()
        self.restored = False
        self.last_backup_ts: float = 0.0
        self.saves: int = 0
        self.uploaded_bytes: int = 0
        self.last_error: str = ""
        self._safe_to_upload = True
        self._last_digest: str = ""

    def restore_on_startup(self) -> bool:
        """Скачивает bot.db.gz из B2 при старте. Если ключа нет — первый запуск.
        При ошибке сети/авторизации отключает выгрузку, чтобы не затереть удалённую БД пустой."""
        if not self.enabled:
            return False
        from botocore.exceptions import ClientError

        bucket = os.getenv("S3_BUCKET", "").strip()
        key = _remote_key(self.remote_name)
        try:
            resp = _client().get_object(Bucket=bucket, Key=key)
            raw = gzip.decompress(resp["Body"].read())
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.db_path.with_suffix(".restore.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, self.db_path)
            self.restored = True
            log.info("БД восстановлена из S3", extra={"key": key, "bytes": len(raw)})
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                log.info("Бэкап в S3 ещё не существует — старт с новой БД", extra={"key": key})
                return False
            self._safe_to_upload = False
            self.last_error = f"restore {code}: {e}"
            log.error("Ошибка восстановления БД из S3; авто-бэкап отключён для защиты данных", extra={"error": self.last_error})
            return False
        except Exception as e:
            self._safe_to_upload = False
            self.last_error = f"restore {type(e).__name__}: {e}"
            log.error("Ошибка восстановления БД из S3; авто-бэкап отключён", extra={"error": self.last_error})
            return False

    @staticmethod
    def _compact_snapshot_bytes(src_path: Path, db=None) -> bytes:
        """Создаёт компактный снимок SQLite без публичного кэша свечей (candles докачиваются с биржи)."""
        fd, tmp_name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            if db is not None and hasattr(db, "_conn") and hasattr(db, "_lock"):
                with db._lock:
                    dst = sqlite3.connect(str(tmp_path))
                    try:
                        db._conn.backup(dst)
                    finally:
                        dst.close()
            else:
                src = sqlite3.connect(str(src_path))
                dst = sqlite3.connect(str(tmp_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()

            # Очищаем публичный кэш свечей только во временной копии и сжимаем файл БД
            compact = sqlite3.connect(str(tmp_path))
            try:
                compact.execute("PRAGMA journal_mode=DELETE")
                try:
                    compact.execute("DELETE FROM candles")
                    compact.commit()
                except sqlite3.OperationalError:
                    pass
                compact.execute("VACUUM")
                compact.commit()
            finally:
                compact.close()

            return tmp_path.read_bytes()
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def backup_now(self, db=None, force: bool = False) -> bool:
        """Безопасный горячий бэкап SQLite (без кэша candles, с пропуском неизменённой БД) -> gzip -> S3."""
        if not self.enabled or not self._safe_to_upload or not self.db_path.exists():
            return False
        bucket = os.getenv("S3_BUCKET", "").strip()
        key = _remote_key(self.remote_name)
        try:
            raw = self._compact_snapshot_bytes(self.db_path, db=db)
            digest = hashlib.sha256(raw).hexdigest()
            if not force and digest == self._last_digest:
                log.debug("S3-бэкап пропущен: состояние БД не изменилось")
                return True
            payload = gzip.compress(raw, compresslevel=9)
            _client().put_object(Bucket=bucket, Key=key, Body=payload)
            self._last_digest = digest
            self.last_backup_ts = time.time()
            self.saves += 1
            self.uploaded_bytes += len(payload)
            self.last_error = ""
            log.info("БД сохранена в S3", extra={"key": key, "gz_kb": round(len(payload) / 1024, 1)})
            return True
        except Exception as e:
            self.last_error = f"backup {type(e).__name__}: {e}"
            log.warning("Не удалось выгрузить бэкап в S3", extra={"error": self.last_error})
            return False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "safe_to_upload": self._safe_to_upload,
            "restored": self.restored,
            "saves": self.saves,
            "uploaded_mb": round(self.uploaded_bytes / 1e6, 3),
            "last_backup_ts": self.last_backup_ts,
            "last_error": self.last_error,
        }


async def s3_backup_loop(stop: asyncio.Event, sync: S3SyncManager, db, interval_sec: float = 3600.0) -> None:
    if not sync.enabled:
        await stop.wait()
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
            break
        except asyncio.TimeoutError:
            await asyncio.to_thread(sync.backup_now, db)


def backup_file_to_s3(local_path: str | Path, remote_name: str) -> bool:
    return S3SyncManager(local_path, remote_name).backup_now()
