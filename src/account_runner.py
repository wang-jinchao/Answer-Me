import logging
import traceback
from browser import Browser

logger = logging.getLogger(__name__)


def run_account(account, url, timeout: int = 30):
    """Run automation for a single account.

    This provides a small, safe skeleton for running UI automation for an account.
    Domain-specific automation steps should be implemented where indicated.

    Returns a dict with at least: user, status, details (optional).
    """
    result = {
        "user": account.get("name") or account.get("username"),
        "status": "failed",
    }

    # Use context-managed Browser to ensure resources are cleaned up.
    try:
        with Browser() as page:
            # Navigate to the target URL
            page.goto(url, timeout=timeout * 1000)

            # --- Domain-specific automation placeholder ---
            # This repository previously had a TODO here. Implement the actions
            # required to log in / perform checks for your application.
            # Example minimal-safe pattern (non-exhaustive):
            # - If account provides username/password, attempt to fill common fields
            # - Click buttons with common selectors
            # Note: selectors below are generic and might not match the real site.

            username = account.get("username")
            password = account.get("password")
            if username and password:
                # Try some common username/email selectors (best-effort)
                try:
                    if page.query_selector('input[name="username"]'):
                        page.fill('input[name="username"]', username)
                    elif page.query_selector('input[name="email"]'):
                        page.fill('input[name="email"]', username)
                    elif page.query_selector('input[type="text"]'):
                        page.fill('input[type="text"]', username)

                    if page.query_selector('input[name="password"]'):
                        page.fill('input[name="password"]', password)
                    elif page.query_selector('input[type="password"]'):
                        page.fill('input[type="password"]', password)

                    # Try to click a submit/login button if present
                    btn = page.query_selector('button[type="submit"]') or page.query_selector('input[type="submit"]')
                    if btn:
                        btn.click()
                except Exception as e:
                    # Keep going — this is best-effort and should not crash the whole run.
                    logger.debug("Login attempt best-effort failed: %s", e, exc_info=True)

            # If you have additional checks, add them here and set result accordingly.
            # For now we mark success if navigation didn't raise and page loaded.
            result["status"] = "success"

    except Exception as e:
        # Narrowed to general Exception here because Playwright may raise many types.
        tb = traceback.format_exc()
        logger.exception("Error running account %s", account.get("name") or account.get("username"))
        result["error"] = str(e)
        result["traceback"] = tb
    return result