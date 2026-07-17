import os
import json

TARGET_URL = "https://www.upfitapp.com/year2024/rsbindex0823"
ACCOUNTS_JSON = [
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
]

def load_accounts():
    return json.loads(
        os.environ["ACCOUNTS_JSON"]
    )

def get_url():
    return os.environ["TARGET_URL"]

