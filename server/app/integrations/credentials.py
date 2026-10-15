import base64
import hashlib
import json
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
from .base import ConnectorError


def _cipher():
    key = hashlib.sha256(("library:accounts:v1:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_credentials(value: dict) -> str:
    return _cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def decrypt_credentials(value: str) -> dict:
    try:
        return json.loads(_cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ConnectorError("账号凭据无法解密；请使用原密钥恢复或重新添加账号") from exc
