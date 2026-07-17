# NOTE: Do NOT store credentials in source. Provide ACCOUNTS_JSON and TARGET_URL via environment variables or CI secrets.
import os
import json

TARGET_URL = "https://www.upfitapp.com/year2024/rsbindex0823"
ACCOUNTS_JSON = "[
  {
    "name":"user001",
    "username":"RS2789",
    "password":"111111"
  },
  {
    "name":"user002",
    "username":"RS2790",
    "password":"111111"
  }
]"

def load_accounts():
    """Load accounts from the ACCOUNTS_JSON environment variable.

    ACCOUNTS_JSON must be a JSON string encoded as an environment variable (for CI use secrets).
    Raises a RuntimeError with a clear message if the variable is missing or invalid.
    """
    s = os.environ.get("ACCOUNTS_JSON")
    if not s:
        raise RuntimeError("ACCOUNTS_JSON environment variable is not set. Provide a JSON string of accounts (see README or CI secrets).")
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ACCOUNTS_JSON is not valid JSON: {e}")


def get_url():
    """Return TARGET_URL from environment or raise a helpful error."""
    url = os.environ.get("TARGET_URL")
    if not url:
        raise RuntimeError("TARGET_URL environment variable is not set.")
    return url
