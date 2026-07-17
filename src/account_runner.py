import json
import logging
import re
import time
import traceback
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

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
    patterns = [
        r"key_base\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"iv_base\s*[:=]\s*['\"]([^'\"]+)['\"]",
    ]
    m = re.search(patterns[0], html)
    if m:
        key = m.group(1)
    m = re.search(patterns[1], html)
    if m:
        iv = m.group(1)
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
    try:
        cookies = page.context.cookies()
    except Exception:
        cookies = []
    for c in cookies:
        if c.get("name") == "PHPSESSID":
            return c.get("value")
    return None


def _click_text(page, text, timeout: int = 10000) -> bool:
    try:
        locator = page.get_by_text(text)
        locator.first.click(timeout=timeout)
        return True
    except Exception:
        logger.debug("Could not click text '%s'", text)
        return False


def _click_button_by_text(page, texts, timeout: int = 10000) -> bool:
    for text in texts:
        if _click_text(page, text, timeout=timeout):
            return True
    return False


def _normalize_param(params, name):
    values = params.get(name)
    if not values:
        return None
    return values[0]


def _fetch_json(page, url):
    return page.evaluate("url => fetch(url, {credentials: 'same-origin'}).then(r => r.json())", url)


def _get_search_result(page, selectors):
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element:
                return element.inner_text().strip()
        except Exception:
            continue
    return ""


def _find_correct_option_uuid(page):
    script = '''

() => {
  const rightList = window.rightList || [];
  const normalized = new Set(rightList.map(item => {
    if (item == null) return null;
    if (typeof item === 'object') return item.uuid || item.id || item.value;
    return item;
  }).filter(Boolean));
  if (normalized.size === 0) return null;
  if (typeof encrypt !== 'function') return null;
  const selectors = ['[data-uuid]', '[uuid]', '[data-id]', 'input[type=radio]', 'input[type=checkbox]'];
  for (const selector of selectors) {
    const nodes = Array.from(document.querySelectorAll(selector));
    for (const node of nodes) {
      const uuid = node.dataset.uuid || node.getAttribute('uuid') || node.getAttribute('data-id') || (node.tagName === 'INPUT' ? node.value : null);
      if (!uuid) continue;
      try {
        const enc = encrypt(uuid);
        if (normalized.has(enc)) return uuid;
      } catch (err) {
        continue;
      }
    }
  }
  return null;
}
'''

    return page.evaluate(script)


def _click_option_by_uuid(page, uuid):
    script = '''

uuid => {
  const selectors = [`[data-uuid=\\"${uuid}\\"]`, `[uuid=\\"${uuid}\\"]`, `[data-id=\\"${uuid}\\"]`];
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node) { node.click(); return true; }
  }
  const radio = Array.from(document.querySelectorAll('input[type=radio], input[type=checkbox]')).find(el => el.value === uuid);
  if (radio) { radio.click(); return true; }
  return false;
}
'''

    return page.evaluate(script, uuid)


def _capture_quiz_summary(page):
    script = '''

() => {
  const questionSelectors = ['.question-title', '.question', '.tit', '.title', '.question-text', '.question-header'];
  let question = '';
  for (const sel of questionSelectors) {
    const el = document.querySelector(sel);
    if (el && el.innerText.trim()) { question = el.innerText.trim(); break; }
  }
  if (!question) {
    const heading = document.querySelector('h2, h3, .title');
    if (heading) question = heading.innerText.trim();
  }
  const options = [];
  const nodes = Array.from(document.querySelectorAll('[data-uuid], [uuid], [data-id], input[type=radio], input[type=checkbox]'));
  for (const node of nodes) {
    const uuid = node.dataset.uuid || node.getAttribute('uuid') || node.getAttribute('data-id') || (node.tagName === 'INPUT' ? node.value : null);
    if (!uuid) continue;
    const text = (node.innerText || node.value || '').trim();
    options.push({uuid, text});
  }
  return {question, options};
}
'''

    return page.evaluate(script)


def _wait_for_reload(page, timeout: int = 10000):
    try:
        page.wait_for_load_state('networkidle', timeout=timeout)
    except Exception:
        pass


def _skip_if_no_attempts(page):
    try:
        return page.locator('text=剩余0次可答').count() > 0
    except Exception:
        return False


def _run_quiz(page, task_label, max_questions, homepage_url):
    details = {
        'status': 'skipped',
        'answered': 0,
        'questions': [],
    }

    page.goto(homepage_url, timeout=10000)
    if not _click_text(page, task_label):
        details['status'] = 'failed'
        details['reason'] = f"Could not find {task_label} link"
        return details

    _wait_for_reload(page, timeout=5000)
    time.sleep(1)

    if _skip_if_no_attempts(page):
        details['status'] = 'no_attempts'
        return details

    if not _click_button_by_text(page, ['开始答题', '开始', '立即答题', '开始练习', '开始学习']):
        details['status'] = 'failed'
        details['reason'] = 'Could not click start button'
        return details

    time.sleep(1)

    for attempt in range(max_questions):
        summary = _capture_quiz_summary(page)
        right_uuid = _find_correct_option_uuid(page)
        question_record = {
            'question': summary.get('question'),
            'options': summary.get('options'),
            'selected_uuid': None,
            'correct_uuid': right_uuid,
        }
        if not right_uuid:
            details['status'] = 'failed'
            details['reason'] = 'Could not determine correct answer'
            details['questions'].append(question_record)
            return details

        clicked = _click_option_by_uuid(page, right_uuid)
        question_record['selected_uuid'] = right_uuid
        details['questions'].append(question_record)
        details['answered'] += 1

        if not clicked:
            details['status'] = 'failed'
            details['reason'] = f'Could not click option {right_uuid}'
            return details

        if not _click_button_by_text(page, ['确定', '下一题', '确认', '提交', '完成']):
            time.sleep(1)
        else:
            time.sleep(1)

        if attempt + 1 >= max_questions:
            break

    details['status'] = 'done'
    return details


def _build_next_daily_url(last_href, base_url):
    parsed = urlparse(last_href)
    params = parse_qs(parsed.query)
    assign = _normalize_param(params, 'assign')
    cntid = _normalize_param(params, 'cntid')
    subTask = _normalize_param(params, 'subTask')
    daily = _normalize_param(params, 'daily')
    draw = _normalize_param(params, 'draw')
    ptaskid = _normalize_param(params, 'ptaskid')
    if not all([assign, cntid, subTask, daily, draw]):
        raise RuntimeError('Missing required parameters for 日常一学 URL construction')
    next_params = {
        'assign': assign,
        'taskid': _normalize_param(params, 'taskid') or '',
        'appid': _normalize_param(params, 'appid') or '',
        'cntid': cntid,
        'daily': str(int(daily) + 1),
        'subTask': str(int(subTask) + 2),
        'draw': str(int(draw) + 1),
        'ptaskid': ptaskid or '',
    }
    path = parsed.path or '/mtask/activedaily'
    new_query = urlencode(next_params)
    return urljoin(base_url, f"{path}?{new_query}"), assign, cntid, next_params['daily'], next_params['subTask'], next_params['ptaskid']


def _daily_learn(page, result, homepage_url):
    details = {'status': 'failed'}
    page.goto(homepage_url, timeout=10000)
    if not _click_text(page, '每日一学'):
        details['status'] = 'failed'
        details['reason'] = 'Could not open 每日一学'
        result['daily_learn'] = details
        return

    _wait_for_reload(page, timeout=5000)
    try:
        page.wait_for_selector('.active__done', timeout=10000)
    except Exception:
        pass

    elements = page.query_selector_all('.active__done a')
    if not elements:
        elements = page.query_selector_all('.active__done')
    if not elements:
        details['status'] = 'failed'
        details['reason'] = 'No active__done elements found'
        result['daily_learn'] = details
        return

    last = elements[-1]
    href = last.get_attribute('href') or ''
    if not href:
        parent = last.query_selector('a')
        href = parent.get_attribute('href') if parent else ''
    if not href:
        details['status'] = 'failed'
        details['reason'] = 'No href found for daily learn activity'
        result['daily_learn'] = details
        return

    try:
        next_url, assign, cntid, daily, subTask, ptaskid = _build_next_daily_url(href, page.url)
        page.goto(next_url, timeout=15000)
        time.sleep(35)
        api_url = f"/operate/ajax/task/markmultitask?assign={assign}&uuid={subTask}&daily={daily}&cntid={cntid}&appid=&ptaskid={ptaskid}"
        api_result = _fetch_json(page, api_url)
        success = False
        if isinstance(api_result, dict):
            success = api_result.get('err') == 200 or api_result.get('error') == 200
        details['api_result'] = api_result
        details['status'] = 'done' if success else 'failed'
        if not success:
            details['reason'] = 'Task API did not return err=200'
    except Exception as e:
        details['status'] = 'failed'
        details['reason'] = str(e)

    result['daily_learn'] = details


def _daily_view(page, result, homepage_url):
    details = {'status': 'failed'}
    page.goto(homepage_url, timeout=10000)
    if not _click_text(page, '每日一看'):
        details['status'] = 'failed'
        details['reason'] = 'Could not open 每日一看'
        result['daily_view'] = details
        return

    time.sleep(5)
    details['status'] = 'done'
    result['daily_view'] = details


def _collect_final_points(page, result):
    try:
        page.wait_for_selector('.topIntegral-number', timeout=10000)
        pts_text = page.query_selector('.topIntegral-number').inner_text()
        m = re.search(r"\d+", pts_text)
        final_points = int(m.group(0)) if m else None
        result['final_points'] = final_points
    except Exception as e:
        logger.warning('Could not read final points: %s', e)
        result['final_points'] = None


def run_account(account, url, timeout: int = 30, captcha_solver=None):
    result = {
        'user': account.get('name') or account.get('username'),
        'status': 'failed',
        'tasks': {},
    }
    username = account.get('username')
    password = account.get('password')

    try:
        with Browser() as page:
            page.goto(url, timeout=timeout * 1000)
            html = page.content()
            key_base, iv_base = _extract_key_iv_from_html(html)
            logger.debug('Extracted key_base=%s iv_base=%s', bool(key_base), bool(iv_base))
            phpsess = _get_php_sessid_from_context(page)

            captcha_text = None
            if phpsess:
                try:
                    img = download_captcha_with_session('PHPSESSID', phpsess)
                    captcha_text = captcha_solver(img) if captcha_solver else ocr_captcha(img)
                except Exception as e:
                    logger.warning('Failed captcha download/ocr via PHPSESSID: %s', e)

            if not captcha_text:
                try:
                    img_el = page.query_selector('img[src*="captcha"]')
                    if img_el:
                        src = img_el.get_attribute('src')
                        if src and src.startswith('/'):
                            src = f'https://www.upfitapp.com{src}'
                        import requests as _req
                        resp = _req.get(src, timeout=15)
                        resp.raise_for_status()
                        captcha_text = captcha_solver(resp.content) if captcha_solver else ocr_captcha(resp.content)
                except Exception as e:
                    logger.warning('Fallback captcha download/ocr failed: %s', e)

            if not captcha_text:
                raise RuntimeError('Failed to obtain captcha text. Provide a captcha_solver or ensure tesseract is installed for OCR.')

            logger.info('Captcha recognized as: %s', captcha_text)

            encrypted_pwd = None
            try:
                has_encrypt = page.evaluate('typeof encrypt === "function"')
            except Exception:
                has_encrypt = False

            payload = {'username': username, 'password': password}
            if has_encrypt:
                try:
                    encrypted_pwd = page.evaluate('(payload) => encrypt(payload)', payload)
                except Exception:
                    encrypted_pwd = None
            if not encrypted_pwd and key_base and iv_base:
                encrypted_pwd = aes_encrypt_for_frontend(payload, key_base, iv_base)

            try:
                if page.query_selector('input[name="username"]'):
                    page.fill('input[name="username"]', username)
                elif page.query_selector('input[name="email"]'):
                    page.fill('input[name="email"]', username)
            except Exception:
                logger.debug('Failed to fill username field')

            try:
                if encrypted_pwd and page.query_selector('input[name="encryptPwd"]'):
                    page.fill('input[name="encryptPwd"]', encrypted_pwd)
                elif page.query_selector('input[name="password"]'):
                    if encrypted_pwd:
                        try:
                            page.fill('input[name="password"]', encrypted_pwd)
                        except Exception:
                            page.fill('input[name="password"]', password)
                    else:
                        page.fill('input[name="password"]', password)
            except Exception:
                logger.debug('Failed to fill password field')

            try:
                if page.query_selector('input[name="captcha"]'):
                    page.fill('input[name="captcha"]', captcha_text)
                elif page.query_selector('input[name="verifyCode"]'):
                    page.fill('input[name="verifyCode"]', captcha_text)
            except Exception:
                logger.debug('Failed to fill captcha field')

            submitted = False
            try:
                btn = page.query_selector('button[type="submit"]') or page.query_selector('input[type="submit"]')
                if btn:
                    btn.click()
                    submitted = True
            except Exception:
                logger.debug('Failed to click submit button')

            if not submitted:
                try:
                    page.keyboard.press('Enter')
                    submitted = True
                except Exception:
                    logger.debug('Failed to submit via Enter')

            _wait_for_reload(page, timeout=10000)
            try:
                page.wait_for_selector('.topIntegral-number', timeout=15000)
                pts_text = page.query_selector('.topIntegral-number').inner_text()
                m = re.search(r'\d+', pts_text)
                result['initial_points'] = int(m.group(0)) if m else None
            except Exception:
                logger.warning('Could not read initial points after login')
                result['initial_points'] = None

            result['tasks']['login'] = {'status': 'done'}
            _daily_learn(page, result['tasks'], url)
            _daily_view(page, result['tasks'], url)
            result['tasks']['daily_practice'] = _run_quiz(page, '每日一练', 10, url)
            result['tasks']['daily_answer'] = _run_quiz(page, '每日一答', 5, url)
            _collect_final_points(page, result)
            result['status'] = 'success'

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception('Error during account workflow for %s', account.get('name') or account.get('username'))
        result['error'] = str(e)
        result['traceback'] = tb

    return result
