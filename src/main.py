import logging
import os
import time

from browser import Browser
from config import load_accounts, get_urls
from account_runner import run_account, _random_sleep
from report import save


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("automation")

RETRYABLE_ERROR_PATTERNS = ("Page.goto", "net::ERR", "ERR_TIMED_OUT")
RETRY_COOLDOWN_SECONDS = 200


def _is_transient_network_error(res):
    """该账号失败是否为瞬时网络故障（导航超时/连接层错误），值得整账号重跑一次。

    只匹配网络类错误；密码错、页面逻辑失败等确定性错误不重跑，
    失败应如实暴露（与 P0 的如实上报哲学一致）。
    """
    if not isinstance(res, dict) or res.get("status") != "failed":
        return False
    err = str(res.get("error") or "")
    return any(p in err for p in RETRYABLE_ERROR_PATTERNS)


def main():
    # 启动前随机等待（cron-job 错峰）：0~3600 秒，避免每次都在同一时刻打到 upfitapp。
    _random_sleep(0, 3600, "启动前随机等待 (cron 错峰)")
    accounts = load_accounts()
    urls = get_urls()




    logger.info("Running %d accounts × %d urls sequentially", len(accounts), len(urls))

    results = []
    for acc in accounts:
        # 每个账号一个浏览器会话：首个 URL 登录后，同账号其余 URL 复用已登录会话（同域 cookie 共享），
        # 避免在第二个活动页重新登录（其登录/验证码机制可能不同）。更换账号才新开浏览器。
        browser = Browser()
        page = browser.start()
        try:
            for i, url in enumerate(urls):
                first_url = (i == 0)
                is_last_url = (i == len(urls) - 1)
                try:
                    res = run_account(acc, url, walk_once=first_url, reuse_page=page, compare_snapshot=first_url, preflight=first_url, is_last_url=is_last_url)
                except Exception:
                    logger.exception("Unhandled exception running account %s", acc.get("name") or acc.get("username"))
                    res = {"user": acc.get("name") or acc.get("username"), "status": "failed", "error": "unhandled exception"}
                logger.info("Account %s finished with status: %s", res.get("user"), res.get("status"))

                if _is_transient_network_error(res):
                    logger.warning(
                        "Account %s hit transient network error, cooling down %ds before full retry (err: %.120s)",
                        res.get("user"), RETRY_COOLDOWN_SECONDS, str(res.get("error") or ""),
                    )
                    time.sleep(RETRY_COOLDOWN_SECONDS)
                    try:
                        res2 = run_account(acc, url, walk_once=first_url, reuse_page=page, compare_snapshot=first_url, preflight=first_url, is_last_url=is_last_url)
                    except Exception:
                        logger.exception("Retry crashed for account %s", acc.get("name") or acc.get("username"))
                        res2 = {
                            "user": acc.get("name") or acc.get("username"),
                            "status": "failed",
                            "error": "unhandled exception on retry",
                        }
                    res2["retried"] = True
                    res2["first_attempt_error"] = res.get("error")
                    res = res2
                    logger.info("Account %s retry finished with status: %s", res.get("user"), res.get("status"))

                results.append(res)
        finally:
            browser.close()

    save(results)


if __name__ == "__main__":
    main()