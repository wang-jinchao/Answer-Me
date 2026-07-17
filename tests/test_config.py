import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

import pytest
from config import load_accounts, get_url


def test_load_accounts_missing_env(monkeypatch):
    monkeypatch.delenv('ACCOUNTS_JSON', raising=False)
    with pytest.raises(RuntimeError, match='ACCOUNTS_JSON environment variable is not set'):
        load_accounts()


def test_get_url_missing_env(monkeypatch):
    monkeypatch.delenv('TARGET_URL', raising=False)
    with pytest.raises(RuntimeError, match='TARGET_URL environment variable is not set'):
        get_url()
