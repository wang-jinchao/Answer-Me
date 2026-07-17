import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    results = []

    max_workers = int(os.environ.get("MAX_WORKERS", "4"))
    logger.info("Running %d accounts with %d workers", len(accounts), max_workers)

    # Use ThreadPoolExecutor with a separate Browser per task (Browser.start uses sync_playwright())
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(run_account, acc, url): acc for acc in accounts}
        for fut in as_completed(futures):
            acc = futures[fut]
            try:
                res = fut.result()
            except Exception:
                logger.exception("Unhandled exception running account %s", acc.get("name") or acc.get("username"))
                res = {"user": acc.get("name") or acc.get("username"), "status": "failed", "error": "unhandled exception"}
            logger.info("Account %s finished with status: %s", res.get("user"), res.get("status"))
            results.append(res)

    save(results)


if __name__ == "__main__":
    main()