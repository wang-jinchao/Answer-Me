import logging
import traceback
import time
import re
from browser import Browser
from utils import (
    aes_encrypt_for_frontend,
    download_captcha_with_session,
    ocr_captcha,
)

logger = logging.getLogger(__name__)


def _extract_key_iv_from_html(html: str):
    # Try several patterns to find key_base and iv_base in page HTML
    key = None
    iv = None
    # patterns like key_base = '...' or "key_base":"..."
    m = re.search(r"key_base\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    if m:
        key = m.group(1)
    m2 = re.search(r"iv_base\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    if m2:
        iv = m2.group(1)
    # fallback: look for data-key_base attributes
    if not key:
        m = re.search(r"data-key_base=['\"]([^'\"]+)['\"]", html)
        if m:
            key = m.group(1)
    if not iv:
        m = re.search(r"data-iv_base=['\"]([^'\"]+)['\"]", html)
        if m:
            iv = m.group(1)
    return key, iv


def _get_php_sessid_from_context(page):
    # Playwright page.context.cookies() -> list
    try:
        cookies = page.context.cookies()
    except Exception:
        # Older Playwright API fallback
        cookies = []
    for c in cookies:
        if c.get("name") == "PHPSESSID":
            return c.get("value")
    return None


def run_account(account, url, timeout: int = 30, captcha_solver=None):
    """Run automation for a single account, including login and initial points capture.

    captcha_solver: optional callable(image_bytes) -> captcha_text to override OCR.
    """
    result = {
        "user": account.get("name") or account.get("username"),
        "status": "failed",
    }

    username = account.get("username")
    password = account.get("password")

    try:
        with Browser() as page:
            # Visit landing page
            page.goto(url, timeout=timeout * 1000)

            # Extract key_base / iv_base from HTML for encryption
            html = page.content()
            key_base, iv_base = _extract_key_iv_from_html(html)
            logger.debug("Extracted key_base=%s iv_base=%s", bool(key_base), bool(iv_base))

            # Obtain PHPSESSID cookie to download captcha image
            phpsess = _get_php_sessid_from_context(page)

            # Download captcha via requests using cookie if available
            captcha_text = None
            if phpsess:
                try:
                    img = download_captcha_with_session("PHPSESSID", phpsess)
                    if captcha_solver:
                        captcha_text = captcha_solver(img)
                    else:
                        try:
                            captcha_text = ocr_captcha(img)
                        except Exception as e:
                            logger.warning("OCR failed: %s", e)
                except Exception as e:
                    logger.warning("Failed to download captcha using session: %s", e)

            # If captcha_text not obtained, attempt to locate captcha image src in page and download via requests without cookie
            if not captcha_text:
                try:
                    img_el = page.query_selector('img[src*="captcha"]')
                    if img_el:
                        src = img_el.get_attribute("src")
                        # Build absolute URL if needed
                        if src.startswith("/"):
                            src = f"https://www.upfitapp.com{src}"
                        import requests as _req
                        resp = _req.get(src, timeout=15)
                        resp.raise_for_status()
                        img = resp.content
                        if captcha_solver:
                            captcha_text = captcha_solver(img)
                        else:
                            captcha_text = ocr_captcha(img)
                except Exception as e:
                    logger.debug("Fallback captcha download/ocr failed: %s", e)

            if not captcha_text:
                raise RuntimeError("Failed to obtain captcha text. Provide a captcha_solver or ensure tesseract is installed for OCR.")

            logger.info("Captcha recognized as: %s", captcha_text)

            # Prefer using page.evaluate encrypt() if available to match front-end exactly
            encrypted_pwd = None
            try:
                has_encrypt = page.evaluate("typeof encrypt === 'function'")
            except Exception:
                has_encrypt = False

            payload = {"username": username, "password": password}

            if has_encrypt:
                try:
                    encrypted_pwd = page.evaluate("(payload) => encrypt(payload)", payload)
                except Exception:
                    encrypted_pwd = None

            # Fallback to Python AES implementation if no page encrypt or it failed
            if not encrypted_pwd:
                if not key_base or not iv_base:
                    logger.warning("No key/iv found on page; attempting plaintext submission (may fail)")
                else:
                    encrypted_pwd = aes_encrypt_for_frontend(payload, key_base, iv_base)

            # Fill form fields (best-effort selectors)
            try:
                if page.query_selector('input[name="username"]'):
                    page.fill('input[name="username"]', username)
                elif page.query_selector('input[name="email"]'):
                    page.fill('input[name="email"]', username)
            except Exception:
                logger.debug("Failed to fill username field, continuing")

            # If there is a dedicated encrypt field, set it
            try:
                if encrypted_pwd and page.query_selector('input[name="encryptPwd"]'):
                    page.fill('input[name="encryptPwd"]', encrypted_pwd)
                elif page.query_selector('input[name="password"]'):
                    # If encrypt expected in password input, set encrypted value; otherwise set raw password
                    # Try encrypted first if available
                    if encrypted_pwd:
                        try:
                            page.fill('input[name="password"]', encrypted_pwd)
                        except Exception:
                            page.fill('input[name="password"]', password)
                    else:
                        page.fill('input[name="password"]', password)
            except Exception:
                logger.debug("Failed to fill password field")

            # Fill captcha
            try:
                if page.query_selector('input[name="captcha"]'):
                    page.fill('input[name="captcha"]', captcha_text)
                elif page.query_selector('input[name="verifyCode"]'):
                    page.fill('input[name="verifyCode"]', captcha_text)
            except Exception:
                logger.debug("Failed to fill captcha field")

            # Try to submit the form
            submitted = False
            try:
                btn = page.query_selector('button[type="submit"]') or page.query_selector('input[type="submit"]')
                if btn:
                    btn.click()
                    submitted = True
            except Exception:
                logger.debug("Failed to click submit button")

            if not submitted:
                try:
                    # Press Enter in password field
                    page.keyboard.press('Enter')
                    submitted = True
                except Exception:
                    logger.debug("Failed to submit via Enter")

            # Wait for possible navigation or for the points element
            try:
                page.wait_for_selector('.topIntegral-number', timeout=15000)
                pts_text = page.query_selector('.topIntegral-number').inner_text()
                # extract digits
                m = re.search(r"\d+", pts_text)
                initial_points = int(m.group(0)) if m else None
                result['initial_points'] = initial_points
            except Exception:
                logger.warning("Could not read initial points after login; login may have failed")

            # Continue with remaining tasks (marked success for now). The caller will proceed to task flows.
            result['status'] = 'success'

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("Error during login and initial collection for account %s", account.get('name') or account.get('username'))
        result['error'] = str(e)
        result['traceback'] = tb

    return result