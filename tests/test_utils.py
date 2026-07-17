import base64
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from utils import aes_encrypt_for_frontend


def test_aes_encrypt_for_frontend_matches_expected():
    data = {
        "username": "testuser",
        "password": "secret"
    }
    key_bytes = b'0123456789abcdef'
    iv_bytes = b'abcdef0123456789'
    key_base = base64.b64encode(key_bytes).decode('utf-8')
    iv_base = base64.b64encode(iv_bytes).decode('utf-8')

    encrypted = aes_encrypt_for_frontend(data, key_base, iv_base)
    assert isinstance(encrypted, str)
    assert encrypted

    json_text = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    plaintext = base64.b64encode(json_text.encode('utf-8'))
    from Crypto.Cipher import AES
    padded = plaintext + (b'\x00' * ((16 - len(plaintext) % 16) % 16))
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    expected = base64.b64encode(base64.b64encode(cipher.encrypt(padded))).decode('utf-8')
    assert encrypted == expected
