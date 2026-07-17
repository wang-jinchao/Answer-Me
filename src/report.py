import json
import datetime
import os

def save(results):

    os.makedirs(
        "reports",
        exist_ok=True
    )

    filename = os.path.join("reports", f"{datetime.date.today()}.json")

    with open(
        filename,
        "w",
        encoding="utf8"
    ) as f:

        json.dump(
            results,
            f,
            ensure_ascii=False,
            indent=2
        )