import json
import os

import config


class LocalStorage:
    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)

    def load(self, name, default=None):
        p = os.path.join(config.DATA_DIR, name)
        if not os.path.exists(p):
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save(self, name, obj):
        p = os.path.join(config.DATA_DIR, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)


class S3Storage:
    def __init__(self):
        import boto3
        ep = config.S3_ENDPOINT_URL
        # у Backblaze регион берётся из адреса: s3.us-west-004.backblazeb2.com
        region = ep.split("s3.")[1].split(".")[0] if "s3." in ep else None
        self.s3 = boto3.client("s3", endpoint_url=ep, region_name=region,
                               aws_access_key_id=config.S3_ACCESS_KEY,
                               aws_secret_access_key=config.S3_SECRET_KEY)

    def load(self, name, default=None):
        from botocore.exceptions import ClientError
        try:
            r = self.s3.get_object(Bucket=config.S3_BUCKET, Key=config.S3_PREFIX + name)
            return json.loads(r["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return default
            raise  # любая другая ошибка - не стартуем, чтобы не затереть состояние пустым

    def save(self, name, obj):
        self.s3.put_object(Bucket=config.S3_BUCKET, Key=config.S3_PREFIX + name,
                           Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def get_storage():
    if config.USE_S3:
        if all([config.S3_ENDPOINT_URL, config.S3_ACCESS_KEY, config.S3_SECRET_KEY, config.S3_BUCKET]):
            return S3Storage()
        print("[WARN] USE_S3 включён, но S3_* не заданы - данные пишутся локально и пропадут при рестарте!")
    return LocalStorage()
