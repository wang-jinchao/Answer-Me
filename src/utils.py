import base64
import json
import re
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
except Exception:
    AES = None

try:
    import requests
except Exception:
    requests = None

try:
    from PIL import Image
    from io import BytesIO
    import pytesseract
except Exception:
    Image = None
    BytesIO = None
    pytesseract = None


def _zero_pad(data: bytes, block_size: int = 16) -> bytes:
    # Zero padding (pad with '\x00' until multiple of block_size)
    pad_len = (block_size - (len(data) % block_size)) % block_size
    if pad_len == 0:
        return data
    return data + (b"\x00" * pad_len)


def aes_encrypt_for_frontend(data_obj, key_base: str, iv_base: str) -> str:
    """Encrypt data_obj following the site's frontend flow:

    JSON -> Base64 -> AES-CBC (ZeroPadding) -> Base64 -> Base64

    key_base and iv_base are expected to be base64-encoded strings from the page.
    Returns the final Base64 string.
    """
    if AES is None:
        raise RuntimeError("pycryptodome is required for AES encryption. Install with 'pip install pycryptodome'.")

    # Prepare plaintext: JSON -> Base64
    json_text = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
    json_bytes = json_text.encode("utf-8")
    b64_json = base64.b64encode(json_bytes)  # bytes

    # Decode key/iv from base64
    try:
        key = base64.b64decode(key_base)
        iv = base64.b64decode(iv_base)
    except Exception as e:
        raise RuntimeError(f"Failed to decode key_base/iv_base: {e}")

    # Zero-pad the plaintext and encrypt using AES-CBC
    padded = _zero_pad(b64_json, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(padded)

    # Double base64 as described
    b64_encrypted = base64.b64encode(encrypted)
    double_b64 = base64.b64encode(b64_encrypted)

    return double_b64.decode("utf-8")


def download_captcha_with_session(cookie_name: str, cookie_value: str, base_url: str = "https://www.upfitapp.com", t: Optional[int] = None) -> bytes:
    """Download captcha image using requests and provided cookie (PHPSESSID).

    Returns raw image bytes.
    """
    if requests is None:
        raise RuntimeError("requests is required for downloading captcha. Install with 'pip install requests'.")

    if t is None:
        t = int(time.time() * 1000)

    url = f"{base_url}/captcha/code?t={t}"
    sess = requests.Session()
    sess.cookies.set(cookie_name, cookie_value, domain="www.upfitapp.com")
    resp = sess.get(url, timeout=15)
    resp.raise_for_status()
    return resp.content


def ocr_captcha(image_bytes: bytes) -> str:
    """Attempt to OCR a 4-char captcha from image bytes using pytesseract.

    Returns the recognized string (trimmed to 4 alnum chars) or raises RuntimeError if OCR not available or fails.
    """
    if pytesseract is None or Image is None:
        raise RuntimeError("pytesseract and Pillow are required for OCR. Install with 'pip install pytesseract pillow' and ensure tesseract is installed on the system.")

    img = Image.open(BytesIO(image_bytes))
    # Convert to grayscale and increase contrast a bit
    img = img.convert("L")

    # Use tesseract with a config to only look for alphanumerics and possibly 4 chars
    try:
        raw = pytesseract.image_to_string(img, config='--psm 7 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
    except Exception as e:
        raise RuntimeError(f"pytesseract failed: {e}")

    if not raw:
        raise RuntimeError("OCR returned empty result")

    # Clean up result and extract up to 4 alnum characters
    cleaned = re.sub(r'[^0-9A-Za-z]', '', raw).strip()
    if len(cleaned) >= 4:
        return cleaned[:4]
    if cleaned:
        return cleaned
    raise RuntimeError(f"OCR did not find valid characters. Raw OCR output: {raw}")
