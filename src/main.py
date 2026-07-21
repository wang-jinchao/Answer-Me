import logging
import os

from config import load_accounts, get_url
from account_runner import run_account
from report import save


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("automation")


def main():
    accounts = load_accounts()
    url = get_url()




    logger.info("Running %d accounts sequentially (one full flow per account)", len(accounts))

    results = []
    for acc in accounts:
        try:
            res = run_account(acc, url)
        except Exception:
            logger.exception("Unhandled exception running account %s", acc.get("name") or acc.get("username"))
            res = {"user": acc.get("name") or acc.get("username"), "status": "failed", "error": "unhandled exception"}
        logger.info("Account %s finished with status: %s", res.get("user"), res.get("status"))
        results.append(res)

    save(results)


if __name__ == "__main__":
    main()