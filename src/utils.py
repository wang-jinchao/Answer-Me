
import base64
import hashlib
import hmac
import json
import logging
import re

logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
except Exception:
    AES = None





def _zero_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = (block_size - (len(data) % block_size)) % block_size
    return data if pad_len == 0 else data + (b"\x00" * pad_len)


def aes_encrypt_for_frontend(password: str, key_base: str, iv_base: str) -> str:
    if AES is None:
        raise RuntimeError("pycryptodome is required for AES encryption (pip install pycryptodome).")


    json_text = json.dumps({"password": password}, separators=(",", ":"), ensure_ascii=False)
    b64_json = base64.b64encode(json_text.encode("utf-8"))

    key = key_base.encode("utf-8")
    iv = iv_base.encode("utf-8")
    if len(key) not in (16, 24, 32) or len(iv) != 16:
        raise RuntimeError(f"key_base/iv_base 长度异常: key={len(key)} iv={len(iv)}")

    padded = _zero_pad(b64_json, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(padded)

    b64_encrypted = base64.b64encode(encrypted)
    return base64.b64encode(b64_encrypted).decode("utf-8")





def _extract_hash_bytes(value: str):
    value = (value or "").strip()
    if not value:
        return None
    m = re.search(r"([0-9a-fA-F]{64})", value)
    if m:
        try:
            return bytes.fromhex(m.group(1))
        except ValueError:
            pass
    return None


def solve_numeric_captcha_from_cookies(cookies):
    if not cookies:
        return None

    phpsessid = None
    target_hash = None
    target_name = None

    for cookie in cookies:
        name = (cookie.get("name") or "").strip()
        value = (cookie.get("value") or "").strip()
        if not value:
            continue
        if name == "PHPSESSID" and phpsessid is None:
            phpsessid = value
        if name == "captcha_vcode_hash" and target_hash is None:
            target_hash = value
            target_name = name
        elif target_hash is None:
            digest = _extract_hash_bytes(value)
            if digest is not None and name != "PHPSESSID":
                target_hash = value
                target_name = name

    if not phpsessid:
        logger.warning("未找到 PHPSESSID cookie，无法计算 HMAC 验证码")
        return None
    if not target_hash:
        logger.warning("未找到 captcha_vcode_hash cookie，无法破解验证码")
        return None

    logger.debug("破解验证码: PHPSESSID 长度=%d, 目标 cookie=%s", len(phpsessid), target_name)
    key = phpsessid.encode("utf-8")
    for i in range(10000):
        attempt = f"{i:04d}".encode("utf-8")
        digest = hmac.new(key, attempt, hashlib.sha256).hexdigest()
        if digest == target_hash:
            logger.info("验证码命中: %s", attempt.decode("utf-8"))
            return attempt.decode("utf-8")

    logger.warning("10000 次穷举均未命中（站点验证码机制可能已变更）")
    return None
