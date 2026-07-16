import os
import json


def load_accounts():

    return json.loads(
        os.environ["ACCOUNTS_JSON"]
    )



def get_url():

    return os.environ["TARGET_URL"]