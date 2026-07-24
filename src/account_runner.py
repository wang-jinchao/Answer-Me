import hashlib
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from browser import Browser
from utils import (
    aes_encrypt_for_frontend,
    solve_numeric_captcha_from_cookies,
)
from walk import run_walk

logger = logging.getLogger(__name__)


def _extract_key_iv_from_html(html: str):

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
        m = re.search(r"name=['\"]key_base['\"][^>]*value=['\"]([^'\"]+)['\"]", html)
        if m:
            key = m.group(1)
    if not iv:
        m = re.search(r"name=['\"]iv_base['\"][^>]*value=['\"]([^'\"]+)['\"]", html)
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


def _click_text(page, text, timeout: int = 6000) -> bool:
    try:
        # 只匹配“可见”文本节点：模糊文本经常命中隐藏/重复节点，若对 .first 点到
        # 不可点击的那个，Playwright 会对每个候选死等满 timeout（原 10s），导致每题
        # 白白浪费 20~30s、直接拖爆网站答题时限。过滤可见节点后点击瞬时完成。
        # timeout 取 6s：既能等到首页入口/按钮正常出现，又避免隐藏节点无限死等。
        locator = page.get_by_text(text, exact=False).filter(visible=True)
        try:
            locator.first.wait_for(state="visible", timeout=timeout)
        except Exception:
            return False
        locator.first.click(timeout=2000)
        return True
    except Exception:
        logger.debug("Could not click text '%s'", text)
        return False


def _click_button_by_text(page, texts, timeout: int = 1000) -> bool:
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


def _beijing_date_str():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y%m%d")


def _py_encrypt(val, date_str):
    salt = hashlib.md5((str(val) + date_str).encode("utf-8")).hexdigest()
    return hashlib.md5((salt + str(val)).encode("utf-8")).hexdigest()


def _site_date_str(page):
    """取站点本地日期 yyyyMMdd（与页面 encrypt 用的 new Date().Format('yyyyMMdd') 一致）。"""
    try:
        return page.evaluate("new Date().Format('yyyyMMdd')")
    except Exception:
        return _beijing_date_str()


def _current_question(page):
    """读取当前题目的选项与正确项加密 uuid。
    数据模型（来自站点 quiz.js）：
      listArr = 题目数组；curTopic = 当前题（optionOrderBy 已重排选项）；
      curTopic.answer = 选项[{uuid,...}]（DOM 顺序即此顺序）；
      curTopic.right.uuid = 正确项加密值（与 rightList[posNum].uuid 同）。
    """
    return page.evaluate("""() => {
        const box = document.querySelector('[id^="qa__box"]');
        if (!box || !box.__vue__ || !box.__vue__.$data) return null;
        const d = box.__vue__.$data;
        const cur = d.curTopic || (d.listArr && d.listArr[d.posNum]) || null;
        if (!cur) return null;
        const ans = cur.answer || [];
        const options = ans.map((o, i) => ({
            uuid: o && o.uuid != null ? String(o.uuid) : null,
            text: o ? String(o.title || o.content || o.name || o.text || o.label || o.answer || '') : '',
            index: i
        }));
        let correctEnc = null;
        if (cur.right && cur.right.uuid != null) correctEnc = String(cur.right.uuid);
        else if (d.rightList && d.rightList[d.posNum] && d.rightList[d.posNum].uuid != null) correctEnc = String(d.rightList[d.posNum].uuid);
        return {options, correctEnc, posNum: d.posNum};
    }""")


def _resolve_correct_option(page):
    """根据 curTopic.right.uuid 命中 curTopic.answer 中正确选项，返回 {uuid, index, text} 或 None。"""
    cq = _current_question(page)
    if not cq:
        logger.warning("[quiz] 未找到当前题目 Vue 数据（未进入答题页？）")
        return None
    options = cq.get("options") or []
    correct_enc = cq.get("correctEnc")
    if not options:
        logger.warning("[quiz] 当前题选项为空")
        return None
    if not correct_enc:
        logger.warning("[quiz] 当前题正确项加密值为空")
        return None
    date_str = _site_date_str(page)
    for opt in options:
        u = opt.get("uuid")
        if not u:
            continue
        try:
            if _py_encrypt(u, date_str) == correct_enc:
                return {"uuid": u, "index": opt.get("index"), "text": (opt.get("text") or "").strip()}
        except Exception:
            continue
    logger.warning(
        "[quiz] 无选项命中 correctEnc。correctEnc=%s options(样例)=%s date=%s",
        correct_enc, [(o.get('uuid'), o.get('text')) for o in options[:6]], date_str,
    )
    return None


def _quiz_entered(page):
    """是否已真正进入答题（Vue 组件已挂载）。"""
    try:
        return page.evaluate("""() => {
            const b = document.querySelector('[id^="qa__box"]');
            if (b && b.__vue__) return true;
            return !!(window.rightList && window.rightList.length);
        }""")
    except Exception:
        return False


def _quiz_is_analysis(page):
    """是否已进入结算/解析页（无题目可答）。分析页 URL 含 /analysis，或页面出现完成类文案。"""
    try:
        return page.evaluate("""() => {
            const url = location.href || '';
            if (/\\/(analysis|answeranalysis)/i.test(url)) return true;
            const body = document.body;
            const txt = body ? (body.innerText || '') : '';
            const done = ['答题完成', '提交成功', '练习完成', '已完成', '您的得分', '本次答对', '全部答对', '答题结束', '成绩'];
            for (const m of done) { if (txt.indexOf(m) >= 0) return true; }
            return false;
        }""")
    except Exception:
        return False


def _enter_quiz(page):
    """点击“开始答题”进入答题；每点一个候选都验证是否真的进入，进入才返回 True。"""
    candidates = [
        "开始答题", "立即答题", "开始作答", "开始练习", "开始学习",
        "去答题", "进入答题", "去做题", "做任务", "开始", "答题", "练习", "作答",
    ]
    for txt in candidates:
        try:
            loc = page.get_by_text(txt, exact=False)
            if not loc.first.count():
                continue
            loc.first.click(timeout=8000)
        except Exception:
            continue
        page.wait_for_timeout(1500)
        if _quiz_entered(page):
            logger.info("[quiz] 已进入答题（点击文案='%s'）", txt)
            return True
        logger.debug("[quiz] 点击 '%s' 后未进入答题，尝试下一个候选", txt)
    return False


def _click_option(page, correct):
    """按 属性 -> 索引(.answer .item) -> 文本 三级兜底点击正确选项。"""
    uuid = correct.get("uuid")
    index = correct.get("index")
    text = (correct.get("text") or "").strip()
    # 1) 按属性 data-uuid / uuid / data-id / input[value]
    if uuid:
        for sel in [f'[data-uuid="{uuid}"]', f'[uuid="{uuid}"]', f'[data-id="{uuid}"]', f'input[value="{uuid}"]']:
            try:
                node = page.query_selector(sel)
                if node:
                    node.click()
                    return True
            except Exception:
                pass
    # 2) 按索引：选项为 .answer 容器下的 .item（顺序与 curTopic.answer 一致）
    if index is not None:
        try:
            loc = page.locator('[id^="qa__box"] .answer .item')
            if loc.count() > index:
                loc.nth(index).click()
                return True
        except Exception:
            pass
    # 3) 按文本兜底（去掉开头的 ①②③ / A.B. 等前缀后做子串匹配）
    if text:
        search = re.sub(r'^[A-D①-④\.\、\s]+', '', text).strip()
        if search:
            try:
                loc = page.locator('[id^="qa__box"] .answer .item').filter(has_text=search).first
                if loc.count():
                    loc.click()
                    return True
            except Exception:
                pass
    return False


def _log_quiz_diag(page, task_label):
    """答题异常时把关键 DOM 摘要打到日志，便于在稳定环境复现并定位结构。"""
    try:
        info = page.evaluate("""() => {
            const o = {url: location.href, hasQaBox: false, correctEnc: null, options: [], optionEls: []};
            const box = document.querySelector('[id^="qa__box"]');
            if (box && box.__vue__ && box.__vue__.$data) {
                o.hasQaBox = true;
                const d = box.__vue__.$data;
                const cur = d.curTopic || (d.listArr && d.listArr[d.posNum]) || null;
                if (cur) {
                    o.correctEnc = (cur.right && cur.right.uuid != null) ? String(cur.right.uuid)
                                 : (d.rightList && d.rightList[d.posNum] && d.rightList[d.posNum].uuid != null ? String(d.rightList[d.posNum].uuid) : null);
                    o.options = (cur.answer || []).map(x => ({uuid: x && x.uuid != null ? String(x.uuid) : '', text: String(x && (x.title||x.content||x.name||x.text||x.label||x.answer) || '')})).slice(0, 6);
                }
            }
            const ans = document.querySelector('[id^="qa__box"] .answer');
            if (ans) ans.querySelectorAll('.item').forEach((el, i) => {
                if (o.optionEls.length >= 6) return;
                o.optionEls.push({i, tag: el.tagName.toLowerCase(), cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 60), text: (el.innerText || '').trim().slice(0, 40)});
            });
            return o;
        }""")
        logger.warning("[quiz-diag %s] %s", task_label, json.dumps(info, ensure_ascii=False))
    except Exception as e:
        logger.warning("[quiz-diag %s] eval failed: %s", task_label, e)


def _dump_debug_html(page, task_label):
    """答题异常时把页面 HTML 落盘，便于复现并定位 DOM 结构。"""
    try:
        import os
        os.makedirs("debug", exist_ok=True)
        safe = re.sub(r'[^\w]', '_', str(task_label))
        path = f"debug/quiz_debug_{safe}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.warning("[debug] 已保存答题页 HTML -> %s", path)
    except Exception as e:
        logger.warning("[debug] 保存调试 HTML 失败: %s", e)


def _wait_quiz_ready(page, timeout: int = 10000):
    start = time.time()
    while (time.time() - start) * 1000 < timeout:
        try:
            cq = _current_question(page)
            if cq and cq.get("options"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _capture_quiz_summary(page):
    return page.evaluate("""() => {
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
        // 选项无 data-uuid 时，用 Vue curTopic.answer 兜底填充（仅 uuid+文本，用于 report 展示）
        if (!options.length) {
            const box = document.querySelector('[id^="qa__box"]');
            if (box && box.__vue__ && box.__vue__.$data) {
                const d = box.__vue__.$data;
                const cur = d.curTopic || (d.listArr && d.listArr[d.posNum]) || null;
                const ans = cur ? (cur.answer || []) : [];
                for (const opt of ans) {
                    const u = opt && opt.uuid != null ? String(opt.uuid) : null;
                    const t = opt && (opt.title || opt.content || opt.name || opt.text || opt.label || opt.answer) ? String(opt.title || opt.content || opt.name || opt.text || opt.label || opt.answer) : '';
                    if (u) options.push({uuid: u, text: t});
                }
            }
        }
        // 仍为空则用 DOM .answer .item 的文本（无 uuid）
        if (!options.length) {
            const ans = document.querySelector('[id^="qa__box"] .answer');
            if (ans) ans.querySelectorAll('.item').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t) options.push({uuid: '', text: t});
            });
        }
        return {question, options};
    }""")

def _wait_for_reload(page, timeout: int = 10000):
    try:
        page.wait_for_load_state('networkidle', timeout=timeout)
    except Exception:
        pass


def _find_login_error(page):
    error_texts = [
        '验证码错误', '验证码不正确', '验证码有误', '登录失败', '用户名或密码',
        '用户名不存在', '帐号不存在', '请重新输入', '登录异常', '密码错误'
    ]
    for text in error_texts:
        try:
            if page.locator(f'text={text}').count() > 0:
                return text
        except Exception:
            continue
    return None


def _is_login_page(page):
    selectors = [
        'input[name="identifier"]',
        'input[name="code"]',
        '.account-captchaImg',
        '#J-captcha',
        'form[onsubmit*="isSubmit"]',
    ]
    for selector in selectors:
        try:
            if page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


def _wait_for_login(page, login_url, timeout: int = 15000):
    start = time.time()
    while time.time() - start < timeout / 1000.0:
        try:
            if page.query_selector('.topIntegral-number'):
                return True
        except Exception:
            pass

        try:
            if page.url != login_url and not _is_login_page(page):
                return True
        except Exception:
            pass

        error = _find_login_error(page)
        if error:
            logger.warning('Detected login error message: %s', error)
            return False

        time.sleep(0.5)
    try:
        return bool(page.query_selector('.topIntegral-number'))
    except Exception:
        return False


def _refresh_captcha(page):
    try:
        if page.query_selector('.account-refresh'):
            page.click('.account-refresh')
            time.sleep(1)
            return True
    except Exception:
        pass
    try:
        img = page.query_selector('#J-captcha') or page.query_selector('img[src*="captcha"]')
        if img:
            try:
                img.click()
                time.sleep(1)
                return True
            except Exception:
                pass
        page.evaluate("() => { const img = document.querySelector('#J-captcha'); if (img) { img.src = '/captcha/code?t=' + Math.random(); } }")
        time.sleep(1)
        return True
    except Exception:
        return False


def _read_points(page):
    try:
        page.wait_for_selector('.topIntegral-number', timeout=10000)
        pts_text = page.query_selector('.topIntegral-number').inner_text()
        m = re.search(r"\d+", pts_text)
        return int(m.group(0)) if m else None
    except Exception as e:
        logger.warning('读取积分失败: %s', e)
        return None


def _skip_if_no_attempts(page):
    try:
        if page.locator('text=剩余0次可答').count() > 0:
            return True

        if page.locator('text=已完成').count() > 0:
            return True
    except Exception:
        return False
    return False


def _read_quiz_progress(page):
    """Parse '第 N 题 / 共 M 题' from the visible question header.

    Returns (current_index, total), both 1-based / total count or None when not
    parseable. Used to detect mid-quiz skips (e.g. a double 'next' click that
    advances two questions at once, silently dropping a question).
    """
    try:
        txt = page.evaluate(
            "() => {"
            " const sels=['.question-title','.question','.tit','.title','.question-text','.question-header','h2','h3'];"
            " for (const s of sels){ const el=document.querySelector(s); if(el && el.innerText && el.innerText.trim()) return el.innerText; }"
            " return '';"
            "}"
        )
    except Exception:
        return None, None
    if not txt:
        return None, None
    m = re.search(r'第\s*(\d+)\s*题', txt)
    t = re.search(r'共\s*(\d+)\s*题', txt)
    cur = int(m.group(1)) if m else None
    tot = int(t.group(1)) if t else None
    return cur, tot


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

    if not _enter_quiz(page):
        details['status'] = 'failed'
        details['reason'] = 'Could not enter quiz (start button not found / did not open quiz)'
        _log_quiz_diag(page, task_label)
        _dump_debug_html(page, task_label)
        return details

    _wait_quiz_ready(page, timeout=8000)
    time.sleep(0.5)

    last_question = None
    no_progress = 0

    for attempt in range(max_questions):
        summary = _capture_quiz_summary(page)
        correct = _resolve_correct_option(page)
        question_record = {
            'question': summary.get('question'),
            'options': summary.get('options'),
            'selected_uuid': None,
            'correct_uuid': correct['uuid'] if correct else None,
        }
        if not correct:
            # 已进入结算/解析页（选项为空、无正确项）：若已答过题则视为完成
            if details['answered'] > 0 and _quiz_is_analysis(page):
                logger.info('%s 进入结算/解析页，判定为完成（已答 %d 题）', task_label, details['answered'])
                details['status'] = 'done'
                return details
            # 可能停在“确定”后的答案解析过渡页，再尝试点“下一题/提交/完成”推进一次
            if details['answered'] > 0:
                _click_button_by_text(page, ['下一题', '提交', '完成'])
                time.sleep(1.2)
                if _resolve_correct_option(page):
                    continue
                if _quiz_is_analysis(page):
                    details['status'] = 'done'
                    return details
            details['status'] = 'failed'
            details['reason'] = 'Could not determine correct answer'
            details['questions'].append(question_record)
            _log_quiz_diag(page, task_label)
            _dump_debug_html(page, task_label)
            return details

        clicked = _click_option(page, correct)
        question_record['selected_uuid'] = correct['uuid']
        details['questions'].append(question_record)
        details['answered'] += 1
        logger.info('%s 作答: %s | 正确uuid=%s 点击=%s',
                    task_label,
                    (summary.get('question') or '').strip().replace('\n', ' ')[:80],
                    correct['uuid'], clicked)

        if not clicked:
            details['status'] = 'failed'
            details['reason'] = f'Could not click option {correct["uuid"]}'
            _log_quiz_diag(page, task_label)
            _dump_debug_html(page, task_label)
            return details


        # 防跳题：先记录作答前题号；点“确定/确认”锁定答案（该按钮可能直接前进，
        # 也可能仅弹解析）。仅当它未使题目前进时才再点“下一题/提交/完成”，
        # 避免一次循环推进两题而静默漏掉中间一题。
        before_cur, _ = _read_quiz_progress(page)
        _click_button_by_text(page, ['确定', '确认'])
        time.sleep(0.6)
        mid_cur, _ = _read_quiz_progress(page)
        if mid_cur is None or mid_cur == before_cur:
            _click_button_by_text(page, ['下一题', '提交', '完成'])
        time.sleep(1.2)
        after_cur, _ = _read_quiz_progress(page)
        if before_cur is not None and after_cur is not None and after_cur > before_cur + 1:
            logger.warning('%s 检测到跳题：第%d题作答后直接到第%d题，中间第%d题被跳过',
                           task_label, before_cur, after_cur, before_cur + 1)
        elif before_cur is not None and after_cur is not None and after_cur == before_cur:
            logger.warning('%s 第%d题作答后题号未推进（可能卡在解析过渡页）', task_label, before_cur)


        cur_q = (summary.get('question') or '').strip()
        if cur_q and cur_q == last_question:
            no_progress += 1
        else:
            no_progress = 0
        last_question = cur_q
        if no_progress >= 2:
            logger.info('%s 题目未推进，提前结束（已答 %d 题）', task_label, details['answered'])
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


# ---------------------------------------------------------------------------
# 积分明细交叉校验（histscore 页）
# 进入方式：用相对路径 "/wcuser/histscore" 拼接 homepage_url（站点域名来自配置/参数，
#   非写死完整 URL）直接 page.goto，比「点击积分元素 + 回首页」少一次整页导航、更稳定。
# 页面每行结构：.bottom_box .item > .left(span[0]=活动类别, span[1]=任务名)
#                                > .right(span[0]=+得分, span[1]=MM-DD HH:MM:SS)
# 经验证映射：
#   限时活动I + 每日一答      -> daily_answer
#   限时活动I + 练兵比武      -> daily_practice（每日一练）
#   每日一看  + 文章阅读      -> daily_view
#   限时活动III（+ 任意专题名）-> daily_learn（每日一学）
# 注意：得分不固定（练兵比武抓到过 +9/+6），故以「类别+任务名+今日日期」判定，
# 不以具体分数判定。时间为服务器北京时间，需用北京时间比对「今日」。
# ---------------------------------------------------------------------------

def _histscore_match(task_key):
    return {
        'daily_answer': ('限时活动I', '每日一答'),
        'daily_practice': ('限时活动I', '练兵比武'),
        'daily_view': ('每日一看', '文章阅读'),
        'daily_learn': ('限时活动III', None),  # 专题名会变，只按类别判定
    }.get(task_key, (None, None))


def _read_histscore_rows(page, homepage_url=None):
    """读取积分明细页记录（Vue 渲染的 .bottom_box .item）。

    进入方式：用相对路径 "/wcuser/histscore" 拼接 homepage_url 直接 goto
    （站点域名来自配置/参数 url，不把完整站点 URL 写死在代码里）。比「先回首页再点击
    积分元素」少一次整页导航，且不依赖元素可见性/点击成功，更稳定。
    跳转或解析失败则安全退化为返回空列表（调用方会退化为「全部执行一次」）。
    """
    if not homepage_url:
        logger.warning('no homepage_url provided; cannot build histscore URL')
        return []
    histscore_url = urljoin(homepage_url, '/wcuser/histscore')
    try:
        page.goto(histscore_url, timeout=20000)
    except Exception as e:
        logger.warning('goto histscore failed: %s', e)
        return []
    try:
        page.wait_for_selector('.bottom_box .item', timeout=10000)
    except Exception:
        pass
    # 触发 dropload 把首页（最多 15 条）加载完整，避免 Vue 挂载后列表为空
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
    except Exception:
        pass
    rows = page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('.bottom_box .item').forEach(it => {
        const l = it.querySelector('.left');
        const r = it.querySelector('.right');
        const ls = l ? [...l.querySelectorAll('span')].map(s => (s.textContent||'').trim()) : [];
        const rs = r ? [...r.querySelectorAll('span')].map(s => (s.textContent||'').trim()) : [];
        out.push({cat: ls[0]||'', name: ls[1]||'', score: rs[0]||'', time: rs[1]||''});
      });
      return out;
    }""")
    return rows or []


def _verify_task_via_histscore(rows, task_key, today_mmdd):
    cat, name = _histscore_match(task_key)
    if not cat:
        return None
    for row in rows:
        if row.get('cat') != cat:
            continue
        if name and row.get('name') != name:
            continue
        if (row.get('time') or '').startswith(today_mmdd):
            return {'done': True, 'score': row.get('score'), 'time': row.get('time'), 'name': row.get('name')}
    return {'done': False}


def _parse_score(raw):
    """把 histscore 的得分串（如 '+10' / '10' / '10分'）转成 int；失败返回 None。"""
    if raw is None:
        return None
    s = str(raw).replace('分', '').strip().lstrip('+')
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _read_walk_score(rows, today_mmdd):
    """从 histscore 提取今日「每日运动」「步数达标」得分，用于每日一走汇总。

    正常情况两者之和应为 +10。匹配规则：行的时间以今日开头，且
    其「任务名(span[1])」或「类别(span[0])」包含 '每日运动' / '步数达标' 关键字。
    返回 {motion, reach, total, has_both}。
    """
    motion = reach = None
    for row in rows:
        if not (row.get('time') or '').startswith(today_mmdd):
            continue
        nm = row.get('name', '') or ''
        ct = row.get('cat', '') or ''
        if motion is None and ('每日运动' in nm or '每日运动' in ct):
            motion = _parse_score(row.get('score'))
        elif reach is None and ('步数达标' in nm or '步数达标' in ct):
            reach = _parse_score(row.get('score'))
    total = (motion or 0) + (reach or 0)
    return {'motion': motion, 'reach': reach, 'total': total, 'has_both': motion is not None and reach is not None}


def _verify_all_tasks(page, result, homepage_url=None):
    """用积分明细页交叉校验四个任务今日是否已完成（权威信号）。

    作用：
    - 修复每日一学依赖 .active__done 的假阴性（只要今日有限时活动III 记录即判完成）；
    - 发现「页面报完成但积分未到账」的假阳性（页面 done 但明细无今日记录则降级 failed）。
    """
    try:
        # 服务器时间为北京时间，用北京时间比对「今日」
        today_mmdd = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%m-%d")
        rows = _read_histscore_rows(page, homepage_url)
        logger.info('histscore rows=%d today=%s', len(rows), today_mmdd)
        for key in ('daily_learn', 'daily_view', 'daily_practice', 'daily_answer'):
            prev = result['tasks'].get(key)
            if not isinstance(prev, dict):
                continue
            verdict = _verify_task_via_histscore(rows, key, today_mmdd)
            prev['histscore_done'] = bool(verdict and verdict.get('done'))
            if verdict and verdict.get('done'):
                prev['histscore_score'] = verdict.get('score')
                prev['histscore_time'] = verdict.get('time')
                if prev.get('status') != 'done':
                    orig = prev.get('reason')
                    prev['status'] = 'done'
                    prev['verified_by'] = 'histscore'
                    prev['reason'] = 'histscore confirms completed today (%s @ %s); original: %s' % (
                        verdict.get('score'), verdict.get('time'), orig)
                    logger.info('Task %s upgraded to done via histscore (%s @ %s)',
                                key, verdict.get('score'), verdict.get('time'))
                else:
                    prev['verified_by'] = 'histscore'
            else:
                if prev.get('status') == 'done':
                    prev['status'] = 'failed'
                    prev['verified_by'] = 'histscore'
                    prev['reason'] = 'in-page reported done but histscore has no entry for today'
                    logger.warning('Task %s downgraded: in-page done but histscore missing today', key)
    except Exception as e:
        logger.warning('histscore verification failed: %s', e)


def _preflight_skip_plan(page, today_mmdd, homepage_url=None):
    """登录后预检：读取积分明细，返回『今日已得分、可跳过执行』的任务集合。

    返回 {task_key: verdict}。仅当今日 histscore 存在该任务「类别 + 任务名」记录时才
    计入，以积分明细（权威到账信号）为准，避免『页面报告完成但积分未到账』类误判。
    读取出错时返回空集合 -> 退化为「全部执行一次」的安全默认。
    """
    try:
        rows = _read_histscore_rows(page, homepage_url)
        logger.info('preflight histscore rows=%d today=%s', len(rows), today_mmdd)
        plan = {}
        for key in ('daily_learn', 'daily_view', 'daily_practice', 'daily_answer'):
            verdict = _verify_task_via_histscore(rows, key, today_mmdd)
            if verdict and verdict.get('done'):
                plan[key] = verdict
                logger.info('preflight: %s 今日已得分 (%s @ %s) -> 跳过执行',
                            key, verdict.get('score'), verdict.get('time'))
        return plan
    except Exception as e:
        logger.warning('preflight histscore read failed: %s', e)
        return {}


def run_account(account, url, timeout: int = 30):
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
            page.wait_for_selector('form[onsubmit*="isSubmit"]', state='attached', timeout=10000)
            page.wait_for_selector('input[name="identifier"], #J-captcha', state='visible', timeout=10000)
            html = page.content()
            key_base, iv_base = _extract_key_iv_from_html(html)
            logger.debug('Extracted key_base=%s iv_base=%s', bool(key_base), bool(iv_base))

            def _get_captcha_text():
                try:
                    cookies = page.context.cookies()
                    logger.debug('Checking %d cookies for captcha hashes', len(cookies))
                    captcha_text = solve_numeric_captcha_from_cookies(cookies)
                    if captcha_text:
                        logger.info('Solved captcha from cookie hash: %s', captcha_text)
                        return captcha_text
                except Exception as e:
                    logger.warning('Captcha hash solver failed: %s', e)

                try:
                    cookie_header = page.evaluate('document.cookie')
                    # 安全：绝不把完整 document.cookie（含 PHPSESSID 等会话凭证）写入日志，
                    # 否则一旦把 logging 调到 DEBUG，实时会话 cookie 会明文出现在 Actions 运行日志里。
                    logger.debug('document.cookie length=%d (session cookies NOT logged)', len(cookie_header) if cookie_header else 0)
                    if cookie_header:
                        header_cookies = []
                        for pair in cookie_header.split(';'):
                            if '=' not in pair:
                                continue
                            name, value = pair.split('=', 1)
                            header_cookies.append({'name': name.strip(), 'value': value.strip()})
                        captcha_text = solve_numeric_captcha_from_cookies(header_cookies)
                        if captcha_text:
                            logger.info('Solved captcha from document.cookie hash: %s', captcha_text)
                            return captcha_text
                except Exception as e:
                    logger.debug('Could not parse document.cookie for captcha hash: %s', e)

                return None

            def _refresh_and_get_captcha_text():
                for attempt in range(3):
                    captcha_text = _get_captcha_text()
                    if captcha_text:
                        return captcha_text
                    if attempt < 2 and _refresh_captcha(page):
                        logger.info('Refreshing captcha for retry %d', attempt + 1)
                        time.sleep(1)
                    else:
                        break
                return None

            captcha_text = _refresh_and_get_captcha_text()
            if not captcha_text:
                raise RuntimeError('Failed to obtain numeric captcha from cookie hash. OCR fallback has been disabled.')

            logger.info('Captcha recognized as: %s', captcha_text)

            encrypted_pwd = None
            payload = {'username': username, 'password': password}
            try:
                has_get_encrypt = page.evaluate('typeof getEncrypt === "function"')
            except Exception:
                has_get_encrypt = False
            try:
                has_encrypt = page.evaluate('typeof encrypt === "function"')
            except Exception:
                has_encrypt = False
            try:
                has_submit_encrypt = page.query_selector('form[onsubmit*="isSubmit"]') is not None
            except Exception:
                has_submit_encrypt = False

            if has_get_encrypt and key_base and iv_base:
                try:
                    encrypted_pwd = page.evaluate('(payload, key, iv) => getEncrypt(payload, key, iv)', payload, key_base, iv_base)
                except Exception:
                    encrypted_pwd = None

            if not encrypted_pwd and has_encrypt:
                try:
                    encrypted_pwd = page.evaluate('(text) => encrypt(text)', password)
                except Exception:
                    encrypted_pwd = None

            if not encrypted_pwd and key_base and iv_base:
                encrypted_pwd = aes_encrypt_for_frontend(payload, key_base, iv_base)

            def _fill_login_fields():
                try:
                    if page.query_selector('input[name="identifier"]'):
                        page.fill('input[name="identifier"]', username)
                    elif page.query_selector('input[name="username"]'):
                        page.fill('input[name="username"]', username)
                    elif page.query_selector('input[name="email"]'):
                        page.fill('input[name="email"]', username)
                except Exception:
                    logger.debug('Failed to fill username field')

                try:
                    if page.query_selector('input[name="password"]'):
                        if has_submit_encrypt:
                            page.fill('input[name="password"]', password)
                        elif encrypted_pwd:
                            page.fill('input[name="password"]', encrypted_pwd)
                        else:
                            page.fill('input[name="password"]', password)
                except Exception:
                    logger.debug('Failed to fill password field')

                try:
                    if page.query_selector('input[name="code"]'):
                        page.fill('input[name="code"]', captcha_text)
                    elif page.query_selector('input[name="captcha"]'):
                        page.fill('input[name="captcha"]', captcha_text)
                    elif page.query_selector('input[name="verifyCode"]'):
                        page.fill('input[name="verifyCode"]', captcha_text)
                except Exception:
                    logger.debug('Failed to fill captcha field')

            def _submit_login():
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
                return submitted

            def _try_login(captcha_value):
                _fill_login_fields()
                if not _submit_login():
                    raise RuntimeError('Failed to submit login form')
                _wait_for_reload(page, timeout=10000)
                return _wait_for_login(page, url, timeout=15000)

            login_success = False
            for attempt in range(3):
                captcha_text = _refresh_and_get_captcha_text()
                if not captcha_text:
                    break
                logger.info('Captcha recognized as: %s', captcha_text)

                _fill_login_fields()
                if not _submit_login():
                    raise RuntimeError('Failed to submit login form')
                _wait_for_reload(page, timeout=10000)
                login_success = _wait_for_login(page, url, timeout=15000)
                if login_success:
                    break
                logger.warning('Login attempt %d failed, refreshing captcha and retrying', attempt + 1)
                if not _refresh_captcha(page):
                    break
                time.sleep(1)

            if not login_success:
                raise RuntimeError('Login could not be confirmed. Check credentials, captcha recognition, or page structure.')

            page.goto(url, timeout=timeout * 1000)
            _wait_for_reload(page, timeout=10000)
            result['initial_points'] = _read_points(page)
            if result['initial_points'] is None:
                logger.warning('Could not read initial points after login')

            result['tasks']['login'] = {'status': 'done'}

            # 登录后预检：对照积分明细，今日已满分的任务直接跳过，避免重复执行 / 浪费答题时限。
            today_mmdd = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%m-%d")
            skip_plan = _preflight_skip_plan(page, today_mmdd, url)

            def _mark_skipped(key, verdict):
                result['tasks'][key] = {
                    'status': 'done',
                    'skipped': True,
                    'verified_by': 'histscore_preflight',
                    'histscore_score': verdict.get('score'),
                    'histscore_time': verdict.get('time'),
                    'reason': 'histscore shows score for today; task skipped (already full)'
                }

            if 'daily_learn' in skip_plan:
                _mark_skipped('daily_learn', skip_plan['daily_learn'])
            else:
                _daily_learn(page, result['tasks'], url)

            if 'daily_view' in skip_plan:
                _mark_skipped('daily_view', skip_plan['daily_view'])
            else:
                _daily_view(page, result['tasks'], url)

            if 'daily_practice' in skip_plan:
                _mark_skipped('daily_practice', skip_plan['daily_practice'])
            else:
                result['tasks']['daily_practice'] = _run_quiz(page, '每日一练', 10, url)

            if 'daily_answer' in skip_plan:
                _mark_skipped('daily_answer', skip_plan['daily_answer'])
            else:
                result['tasks']['daily_answer'] = _run_quiz(page, '每日一答', 5, url)

            # 用积分明细页交叉校验四个任务今日是否完成（权威信号：修复每日一学假阴性/假阳性）
            _verify_all_tasks(page, result, url)

            # 每日一走：独立于 Web 答题体系，走微信小程序 liteapp openId 接口。
            # 执行顺序：① 先 run_walk 执行步数写入；② 再读 histscore 计算「每日运动+步数达标」得分汇总。
            # 微信运动加密包（enc/iv/key）为运行时注入（环境变量 WALK_ENC/WALK_IV/WALK_KEY），
            # 不写进静态 ACCOUNTS_JSON。仅当账号配置了 openid 才执行；否则标 skipped。
            walk_result = {}
            if account.get("openid"):
                logger.info("账号 %s 配置了 openid，执行每日一走（先写步数，后读 histscore 汇分）",
                            account.get("name") or account.get("username"))
                # 运行时加密包：优先环境变量，不存在则为 None -> run_walk 内部标 skipped
                walk_enc = os.environ.get("WALK_ENC")
                walk_iv = os.environ.get("WALK_IV")
                walk_key = os.environ.get("WALK_KEY")
                try:
                    wk = run_walk(account, enc=walk_enc, iv=walk_iv, key=walk_key)
                except Exception as e:
                    logger.exception("每日一走 run_walk 异常：%s", e)
                    wk = {"status": "failed", "reason": f"run_walk 异常：{e}"}
                # ② 后读 histscore：计算今日「每日运动 + 步数达标」得分汇总（正常 +10）
                try:
                    wrows = _read_histscore_rows(page, url)
                    wscore = _read_walk_score(wrows, today_mmdd)
                    wk["motion_score"] = wscore.get("motion")
                    wk["reach_score"] = wscore.get("reach")
                    wk["today_walk_score"] = wscore.get("total")
                except Exception as e:
                    logger.warning("每日一走 读 histscore 得分汇总失败：%s", e)
                walk_result = wk
            else:
                walk_result = {
                    "status": "skipped",
                    "reason": "账号未配置 openid，跳过每日一走",
                }
            result["tasks"]["daily_walk"] = walk_result

            page.goto(url, timeout=timeout * 1000)
            _wait_for_reload(page, timeout=10000)
            final_points = _read_points(page)
            if final_points is None:
                logger.warning('Could not read final points after returning to homepage')
            result['final_points'] = final_points
            result['status'] = 'success'

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception('Error during account workflow for %s', account.get('name') or account.get('username'))
        result['error'] = str(e)
        result['traceback'] = tb

    return result
