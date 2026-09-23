import hashlib
import json
import logging
import os
import html
import re
import random
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


# 「按钮/链接类」元素选择器：用于限定宽泛候选词的点击范围。
# 背景：_enter_quiz 的候选里 "开始"/"答题"/"练习"/"作答" 这类泛词，若用
# get_by_text 全文匹配，很容易命中页面标题 / 导航文字 / 任务名并点下去，
# 把页面点到别的模块 —— 之后所有诊断抓到的都是「被点飞后的页面」，
# 0820 那次诊断显示 buttons=[] / textHits=[] 很可能就是这么来的。
_CLICKABLE_SELECTOR = (
    "button, a, [role='button'], .btn, [class*='btn'], "
    "[class*='start'], [class*='begin'], [class*='submit']"
)


def _click_text_clickable(page, text, timeout: int = 6000) -> bool:
    """只在按钮/链接类元素内按文本点击（供泛词候选使用，避免点飞页面）。

    与 _click_text 的区别：_click_text 用 get_by_text 匹配任意文本节点，
    泛词会命中标题/导航；这里限定元素类型，命中即视为真按钮。
    """
    try:
        loc = (page.locator(_CLICKABLE_SELECTOR)
                   .filter(has_text=text)
                   .filter(visible=True))
        if not loc.count():
            return False
        loc.first.click(timeout=2000)
        return True
    except Exception:
        logger.debug("Could not click clickable text '%s'", text)
        return False


def _goto(page, url, timeout: int = 15000, retries: int = 3):
    """导航到 url，使用 domcontentloaded（不等整页子资源），失败自动重试。

    Playwright 默认 wait_until='load' 会等所有图片/iframe/脚本加载完才返回，
    一旦某个第三方资源卡住就抛 TimeoutError——而页面其实已可用（目标元素已在 DOM）。
    campaign 页 rsbindex0823 含小程序 webview/统计脚本，load 经常 >10s 偶发超时。
    改用 domcontentloaded 只等 HTML 解析，再由后续 wait_for_selector/_click_text 确保可交互。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return
        except Exception as e:
            last_err = e
            logger.warning('goto %s 第 %d/%d 次超时/失败: %s', url, attempt, retries, e)
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
    logger.error('goto %s 重试 %d 次仍失败', url, retries)
    raise last_err


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


# 答题页 Vue 根节点查找器（纯 JS 函数，供各 evaluate 内联复用）。
# 两套答题页并存：
#   · 老页（rsbindex0823 系列）根节点形如 <div id="qa__box-xxx">；
#   · 新页 bsanswerpro（rsbindex0820「每日一答」）根节点是 <div id="app">，
#     其 __vue__.$data 同样有 curTopic / listArr / posNum。
# 原先代码硬编码 [id^="qa__box"]，在 bsanswerpro 上恒返回 null，表现为
# 「hasQaBox:false / 找不到开始答题按钮」。改为按「谁挂着带 curTopic/listArr 的
# Vue 实例」来找根节点，两套页面通吃；命中顺序保持 qa__box 优先，老页行为不变。
_QUIZ_ROOT_FINDER_JS = r"""
function __quizRoot() {
  var cands = [];
  var qa = document.querySelector('[id^="qa__box"]');
  if (qa) cands.push(qa);
  var app = document.querySelector('#app');
  if (app) cands.push(app);
  var all = document.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) { if (all[i] && all[i].__vue__) cands.push(all[i]); }
  for (var j = 0; j < cands.length; j++) {
    var v = cands[j].__vue__;
    if (!v || !v.$data) continue;
    var d = v.$data;
    if (d.curTopic || (d.listArr && d.listArr.length)) return cands[j];
  }
  return null;
}
"""


def _wait_page_content(page, timeout: int = 15000):
    """等待答题页真正渲染出内容（Vue 挂载完成 / 接口回填）。

    bsanswerpro 页在 domcontentloaded 后仍需数百 ms~数秒才挂载 Vue 并渲染按钮，
    原先点完入口只 sleep(1s) 就开始找「开始答题」，页面此时还是空壳
    （诊断里 buttons=[] / textHits=[] 即此现象）→ 必然判失败。
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            ok = page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
                if (__quizRoot()) return true;
                var t = document.body ? (document.body.innerText || '').trim() : '';
                return t.length > 20;
            }""")
            if ok:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _quiz_already_done(page):
    """答题页显示「今日已完成 / 次数用完」等终态 → 视为今天已做过，不判失败。

    同一天重复跑（GA 手工重跑、或白天已手动答过）时，bsanswerpro 页不会再有
    「开始答题」按钮，此时若报 failed 是假警报。
    """
    markers = [
        '剩余0次可答', '剩余 0 次', '答题次数已用完', '今日答题已完成',
        '今日已完成', '已完成今日', '今日已答', '明天再来', '今日任务已完成',
    ]
    try:
        txt = page.evaluate("() => (document.body ? (document.body.innerText || '') : '')")
    except Exception:
        return False
    for m in markers:
        if m in txt:
            logger.info('[quiz] 检测到已完成态文案「%s」，判定今日已答过', m)
            return True
    return False


def _current_question(page):
    """读取当前题目的选项与正确项加密 uuid。
    数据模型（来自站点 quiz.js）：
      listArr = 题目数组；curTopic = 当前题（optionOrderBy 已重排选项）；
      curTopic.answer = 选项[{uuid,...}]（DOM 顺序即此顺序）；
      curTopic.right.uuid = 正确项加密值（与 rightList[posNum].uuid 同）。
    """
    return page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
        const box = __quizRoot();
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
    """是否已真正进入答题（Vue 组件已挂载，或已出现选项/题干）。"""
    try:
        return page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
            if (__quizRoot()) return true;
            const b = document.querySelector('[id^="qa__box"]');
            if (b && b.__vue__) return true;
            if (window.rightList && window.rightList.length) return true;
            // 兜底：出现选项容器或“第N题”题干即视为已进入（应对 Vue 挂载慢/结构微调）
            if (document.querySelector('[id^="qa__box"] .answer .item')) return true;
            if (document.querySelector('.answer .item')) return true;
            if (document.querySelector('#app .answer .item')) return true;
            const t = (document.body && document.body.innerText) || '';
            if (/第\\s*\\d+\\s*题/.test(t)) return true;
            return false;
        }""")
    except Exception:
        return False


def _quiz_is_analysis(page):
    """是否已进入结算/解析页（无题目可答）。

    关键护栏：只要页面上仍出现可答选项（.answer .item 或 curTopic.answer 非空）
    就一律判为“非结算页”。这样“有题目但被完成类关键词误判”的跳题场景被从根上消除
    （之前正是第 N 题界面残留“成绩/本次答对”等文案时，被误判为结算页 → 提前 done → 漏答）。
    """
    try:
        return page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
            // 1) 页面上仍有可答选项 → 绝不可能是结算页
            if (document.querySelector('[id^="qa__box"] .answer .item')) return false;
            if (document.querySelector('#app .answer .item')) return false;
            const b = __quizRoot() || document.querySelector('[id^="qa__box"]');
            if (b && b.__vue__ && b.__vue__.$data) {
                const d = b.__vue__.$data;
                const cur = d.curTopic || (d.listArr && d.listArr[d.posNum]) || null;
                if (cur && Array.isArray(cur.answer) && cur.answer.length) return false;
            }
            // 2) 真正无题目时，再按 URL / 完成类文案判定
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
    """点击“开始答题”进入答题；每点一个候选都验证是否真的进入，进入才返回 True。
    点击后轮询等待（最多 ~10s）再判进入，避开“Vue 挂载慢于 1.5s”的误判；
    全部候选失败则打印页面真实入口文案，便于定位站点改版。"""
    # 精确候选：语义明确，按原逻辑全文匹配（老页一直靠它命中，行为不变）。
    precise_candidates = [
        "开始答题", "立即答题", "开始作答", "开始练习", "开始学习",
        "去答题", "进入答题", "去做题", "开始挑战",
    ]
    # 泛词候选：只在按钮/链接类元素内点（_click_text_clickable），
    # 否则会命中标题/导航把页面点飞，导致后续诊断抓到的是别的页面。
    wide_candidates = ["做任务", "开始", "答题", "练习", "作答"]
    # 先等页面真正渲染出内容：bsanswerpro 等 Vue 页在 domcontentloaded 后仍需
    # 数百 ms~数秒才挂载，空壳页面上找不到任何按钮（原诊断 buttons=[] 即此现象）。
    _wait_page_content(page, timeout=15000)
    # 点击前先留一份页面状态快照：一旦后面点飞了，日志里仍能看到「刚进来时长啥样」。
    try:
        pre_state = page.evaluate("""() => {
            const t = document.body ? (document.body.innerText || '') : '';
            return {url: location.href, bodyLen: t.length,
                    bodyHead: t.replace(/\\s+/g, ' ').slice(0, 200)};
        }""")
    except Exception:
        pre_state = None
    logger.info("[quiz] 进入答题前页面状态: %s", pre_state)
    tried = []
    # 最多 3 轮扫描：首轮无候选时页面可能仍在异步渲染，等 3s 再扫，避免直接判失败。
    for _round in range(3):
        for txt in precise_candidates:
            try:
                loc = page.get_by_text(txt, exact=False)
                if not loc.first.count():
                    continue
                loc.first.click(timeout=8000)
                tried.append(txt)
            except Exception as e:
                logger.debug("[quiz] 点击候选 '%s' 异常: %s", txt, e)
                continue
            # 轮询等待真正进入（Vue 挂载可能慢），最多 ~10s
            entered = False
            for _ in range(20):
                page.wait_for_timeout(500)
                if _quiz_entered(page):
                    entered = True
                    break
            if entered:
                logger.info("[quiz] 已进入答题（点击文案='%s'）", txt)
                return True
            logger.debug("[quiz] 点击 '%s' 后未进入答题，尝试下一个候选", txt)
        if tried:
            break
        logger.warning("[quiz] 第 %d 轮未扫描到任何精确入口候选，等待 3s 后重试", _round + 1)
        try:
            page.wait_for_timeout(3000)
        except Exception:
            time.sleep(3)

    # 精确候选全部落空 → 才动用泛词，且限定在按钮/链接类元素内点击。
    if not tried:
        for txt in wide_candidates:
            if not _click_text_clickable(page, txt):
                continue
            tried.append('[clickable]' + txt)
            entered = False
            for _ in range(20):
                page.wait_for_timeout(500)
                if _quiz_entered(page):
                    entered = True
                    break
            if entered:
                logger.info("[quiz] 已进入答题（泛词点击文案='%s'）", txt)
                return True

    logger.warning("[quiz] 所有候选入口均未能进入答题，已尝试: %s | 点击前页面状态: %s",
                   tried, pre_state)
    _log_entry_candidates(page)
    return False


def _log_entry_candidates(page):
    """进入答题失败时，抓取页面上所有疑似入口按钮/含关键词的文本，便于定位站点改版。"""
    try:
        data = page.evaluate("""() => {
            const buttons = [];
            const sel = 'button, a, [role="button"], .btn, [class*="start"], [class*="begin"], [class*="entry"]';
            document.querySelectorAll(sel).forEach(el => {
                const t = (el.innerText || el.getAttribute('title') || el.getAttribute('alt') || '').trim();
                if (t && t.length <= 20) buttons.push(t);
            });
            const body = document.body ? document.body.innerText : '';
            const hits = (body.match(/[^\\n]{0,15}(开始|答题|练习|作答|挑战|进入|去)[^\\n]{0,15}/g) || []).slice(0, 25);
            const iframes = Array.from(document.querySelectorAll('iframe')).map(f => f.src || '(no-src)').slice(0, 10);
            return {buttons: [...new Set(buttons)].slice(0, 30), textHits: hits,
                    iframes: iframes, bodyLen: body.length,
                    bodyHead: body.replace(/\\s+/g, ' ').slice(0, 300)};
        }""")
        logger.warning("[quiz-entry-fail] 页面按钮文案: %s | 含关键词文本: %s | iframe: %s | 正文长度=%s 开头=%s",
                       json.dumps(data.get("buttons", []), ensure_ascii=False),
                       json.dumps(data.get("textHits", []), ensure_ascii=False),
                       json.dumps(data.get("iframes", []), ensure_ascii=False),
                       data.get("bodyLen"), data.get("bodyHead"))
    except Exception as e:
        logger.warning("[quiz-entry-fail] 提取入口文案失败: %s", e)


def _try_click_node(node):
    """对一个已定位的 DOM 节点（ElementHandle）尝试点击，按 普通 -> force -> JS 三级兜底，
    绕过被遮罩/未稳定导致的 Playwright actionability 失败（如题间过渡遮罩层拦截点击）。"""
    try:
        node.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    # 1) 普通点击（含 actionability 检查，失败多为被遮挡/未稳定）
    try:
        node.click(timeout=2000)
        return True
    except Exception:
        pass
    # 2) 强制点击（忽略遮挡与稳定检查）
    try:
        node.click(timeout=1500, force=True)
        return True
    except Exception:
        pass
    # 3) JS 直接触发点击（绕过一切 Playwright 拦截，兜底）
    try:
        node.evaluate("el => el.click()")
        return True
    except Exception:
        pass
    return False


def _click_option(page, correct):
    """按 属性 -> 索引(.answer .item) -> 文本 三级兜底点击正确选项；每级内部再做 普通/force/JS 三级点击兜底。"""
    uuid = correct.get("uuid")
    index = correct.get("index")
    text = (correct.get("text") or "").strip()
    # 1) 按属性 data-uuid / uuid / data-id / input[value]
    if uuid:
        for sel in [f'[data-uuid="{uuid}"]', f'[uuid="{uuid}"]', f'[data-id="{uuid}"]', f'input[value="{uuid}"]']:
            try:
                node = page.query_selector(sel)
                if node and _try_click_node(node):
                    return True
            except Exception:
                pass
    # 2) 按索引：选项为 .answer 容器下的 .item（顺序与 curTopic.answer 一致）
    if index is not None:
        try:
            loc = page.locator('[id^="qa__box"] .answer .item')
            if loc.count() > index:
                node = loc.nth(index).element_handle()
                if node and _try_click_node(node):
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
                    node = loc.first.element_handle()
                    if node and _try_click_node(node):
                        return True
            except Exception:
                pass
    # 4) 兜底：在 Vue 根节点内按 uuid / 索引 / 文本点击（覆盖 bsanswerpro 新页）。
    #    放在最后，老页（qa__box）走 1~3 步已命中，行为完全不变。
    try:
        ok = page.evaluate("(a) => {" + _QUIZ_ROOT_FINDER_JS + r"""
            const root = __quizRoot();
            if (!root) return false;
            let items = Array.from(root.querySelectorAll('.answer .item'));
            if (!items.length) items = Array.from(root.querySelectorAll('.answer li'));
            // bsanswerpro 新页（0820 每日一答）选项容器是 .answerCon-list-item，
            // 与老页 .answer .item 完全不同；不补这一级则 items 恒为空，必定点不中。
            if (!items.length) items = Array.from(root.querySelectorAll('.answerCon-list-item'));
            if (!items.length) items = Array.from(root.querySelectorAll('[class*="option"]'));
            let el = null;
            if (a.uuid) {
                el = root.querySelector('[data-uuid="' + a.uuid + '"]') ||
                     root.querySelector('[uuid="' + a.uuid + '"]') ||
                     root.querySelector('[data-id="' + a.uuid + '"]');
            }
            if (!el && a.index != null && items.length > a.index) el = items[a.index];
            if (!el && a.text) {
                const t = String(a.text).replace(/^[A-D①-④.、\s]+/, '').trim();
                if (t) {
                    for (const it of items) {
                        if ((it.innerText || '').indexOf(t) >= 0) { el = it; break; }
                    }
                }
            }
            if (!el) return false;
            try { el.scrollIntoView({block: 'center'}); } catch (e) {}
            el.click();
            return true;
        }""", {"uuid": uuid, "index": index, "text": text or ""})
        if ok:
            return True
    except Exception as e:
        logger.debug("[quiz] 根节点内点击兜底失败: %s", e)
    return False


def _click_quiz_confirm(page):
    """bsanswerpro 新答题页（0820「每日一答」）：选中选项后必须再点底部「确定」才提交，
    点选项本身不会切题。

    老页（qa__box）点选项 = 提交 + 自动切题，且 DOM 里的「确定」只属于提交失败弹窗，
    盲点会把未解析的下一题以垃圾答案提交（08-18 / 09-07 两次缺第 2 题事故即此根因）。
    故这里用「存在 .answerCon-list 选项容器」作为新页判据，老页直接返回 False，绝不点击。
    """
    try:
        return bool(page.evaluate(r"""() => {
            if (!document.querySelector('.answerCon-list')) return false;
            const btn = document.querySelector('.indexFoot-btn');
            if (!btn) return false;
            try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
            btn.click();
            return true;
        }"""))
    except Exception as e:
        logger.debug("[quiz] 新页「确定」提交失败: %s", e)
        return False


def _log_quiz_diag(page, task_label):
    """答题异常时把关键 DOM 摘要打到日志，便于在稳定环境复现并定位结构。"""
    try:
        info = page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
            const o = {url: location.href, hasQaBox: false, correctEnc: null, options: [], optionEls: [],
                       roots: [], bodyLen: 0, bodyHead: ''};
            const box = __quizRoot();
            // 把所有挂了 __vue__ 的根节点 id/class 打出来，便于确认新页（bsanswerpro）的真实结构
            document.querySelectorAll('*').forEach(el => {
                if (el.__vue__ && o.roots.length < 10) {
                    o.roots.push((el.tagName || '').toLowerCase() +
                                 (el.id ? '#' + el.id : '') +
                                 (el.className && el.className.toString ? '.' + el.className.toString().slice(0, 40) : ''));
                }
            });
            const bd = document.body ? (document.body.innerText || '') : '';
            o.bodyLen = bd.length;
            o.bodyHead = bd.replace(/\\s+/g, ' ').slice(0, 300);
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
            const ans = (box && box.querySelector('.answer')) || document.querySelector('[id^="qa__box"] .answer') ||
                        (box && box.querySelector('.answerCon-list')) || document.querySelector('.answerCon-list');
            if (ans) ans.querySelectorAll('.item, .answerCon-list-item').forEach((el, i) => {
                if (o.optionEls.length >= 6) return;
                o.optionEls.push({i, tag: el.tagName.toLowerCase(), cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 60), text: (el.innerText || '').trim().slice(0, 40)});
            });
            return o;
        }""")
        logger.warning("[quiz-diag %s] %s", task_label, json.dumps(info, ensure_ascii=False))
    except Exception as e:
        logger.warning("[quiz-diag %s] eval failed: %s", task_label, e)


def _dump_debug_html(page, task_label):
    """答题异常时把页面调试片段落盘，便于复现并定位 DOM 结构。
    只落『答题框子树』或『正文纯文本快照』，绝不写整页 page.content()，
    避免把登录态页里的昵称 / 嵌入会话令牌等一并落盘造成泄露。"""
    try:
        import os
        os.makedirs("debug", exist_ok=True)
        safe = re.sub(r'[^\w]', '_', str(task_label))
        path = f"debug/quiz_debug_{safe}.html"
        # 优先只取答题框子树（不含 header/nav/脚本里的会话信息）
        snippet = page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
            const box = __quizRoot() || document.querySelector('[id^="qa__box"]');
            if (box) return box.outerHTML;
            // 进入失败等无答题框场景：只取正文纯文本，避免整页脚本/令牌外泄
            const body = document.body ? document.body.innerText : '';
            return '<pre>' + (body || '').replace(/</g, '&lt;') + '</pre>';
        }""")
        with open(path, "w", encoding="utf-8") as f:
            f.write(snippet)
        logger.warning("[debug] 已保存答题页调试片段 -> %s", path)
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
    return page.evaluate("() => {" + _QUIZ_ROOT_FINDER_JS + """
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
            const box = __quizRoot() || document.querySelector('[id^="qa__box"]');
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
            const root = __quizRoot();
            const ans = (root && root.querySelector('.answer')) || document.querySelector('[id^="qa__box"] .answer');
            if (ans) ans.querySelectorAll('.item').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t) options.push({uuid: '', text: t});
            });
        }
        return {question, options};
    }""")

def _wait_for_reload(page, timeout: int = 10000):
    try:
        # 用 domcontentloaded 而非 networkidle：第三方脚本/统计请求常持续不断，
        # networkidle（网络彻底安静）可能永不触发而卡超时——与 _goto 是同源隐患。
        # domcontentloaded 在页面已 loaded 时常立即返回，不阻塞；且每个调用点之后
        # 都有 wait_for_selector 兜底等元素，功能不受影响。
        page.wait_for_load_state('domcontentloaded', timeout=timeout)
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


def _current_pos(page):
    """当前题序号（优先 Vue posNum；回退 DOM 正则「第N题」，1-based）。

    用于判定是否真正前进到下一题。Vue posNum 随切题立即更新，不受解析层
    覆盖 DOM 题号文本影响，比 _read_quiz_progress 的正则更可靠——之前的吞题
    （某题作答后直接跳到后一题、少答一道）正是 DOM 文本在过渡页抖动导致误判。
    """
    try:
        cq = _current_question(page)
        if cq and cq.get('posNum') is not None:
            return cq['posNum']
    except Exception:
        pass
    cur, _ = _read_quiz_progress(page)
    return cur


def _await_advance(page, pos_pre, timeout: int = 5000):
    """轮询等待题目前进（posNum 变大）或进入结算页。

    返回 True 表示已切换到新题或已进入结算；超时返回 False。
    用轮询代替固定 sleep 后读题号，消除过渡页延迟导致的「读早误判」
    （读早→误以为没前进→多补点一次→跳题/吞题）。
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        if _quiz_is_analysis(page):
            return True
        try:
            pos = _current_pos(page)
        except Exception:
            pos = None
        if pos is not None and pos_pre is not None and pos > pos_pre:
            return True
        if pos is not None and pos_pre is None:
            return True
        try:
            page.wait_for_timeout(200)
        except Exception:
            pass
    return False


def _total_questions(page):
    """题目总数（Vue listArr.length）。取不到返回 0，调用方降级用 max_questions。"""
    try:
        return page.evaluate(
            "() => {" + _QUIZ_ROOT_FINDER_JS +
            " const b=__quizRoot();"
            " if(b&&b.__vue__&&b.__vue__.$data&&Array.isArray(b.__vue__.$data.listArr))"
            " return b.__vue__.$data.listArr.length; return 0; }"
        ) or 0
    except Exception:
        return 0


def _wait_resolve_correct(page, timeout: int = 8000):
    """重试解析正确项，吃掉“题目数据尚未就绪”的竞态。

    旧逻辑在循环开头直接 _resolve_correct_option，若此刻 posNum 已切到新题、
    但 curTopic.right.uuid 尚未填充，会返回 None → 被误判为“题间过渡”直接跳下一题，
    导致正在加载的题被静默跳过（偶发跳题根因之一）。这里先短重试再下结论。
    """
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        c = _resolve_correct_option(page)
        if c:
            return c
        if _quiz_is_analysis(page):
            return None
        time.sleep(0.3)
    return None


def _run_quiz(page, task_label, max_questions, homepage_url, skip_if_missing=False):
    details = {
        'status': 'skipped',
        'answered': 0,
        'skipped': 0,
        'expected': 0,
        'questions': [],
    }

    _goto(page, homepage_url, timeout=15000)
    if not _click_text(page, task_label):
        # 诊断：入口文本未命中时，把页面上与答题相关的链接 dump 到日志（含 assign / bsanswerpro），
        # 用于确认第二个活动页「每日一答」的真实绝对地址与 assign 取值规律。
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(a => ((a.textContent || '').trim()) + ' => ' + (a.getAttribute('href') || ''))",
            )
            rel = [h for h in hrefs
                   if any(k in h.lower() for k in ('answer', 'assign', 'bsanswerpro', 'daily'))]
            logger.warning('%s 入口未命中；页面答题相关链接(%d/%d): %s',
                           task_label, len(rel), len(hrefs), rel[:20])
        except Exception as e:
            logger.debug('%s 入口未命中且 dump 链接失败: %s', task_label, e)
        details['status'] = 'skipped' if skip_if_missing else 'failed'
        details['reason'] = (f"未找到 {task_label} 入口，跳过（可能已下线）"
                             if skip_if_missing else f"Could not find {task_label} link")
        return details

    _wait_for_reload(page, timeout=5000)
    # 等页面真正渲染出内容（Vue 挂载）：bsanswerpro 等页 domcontentloaded 后仍需等待，
    # 只 sleep(1s) 就开始找「开始答题」会对着空壳页面扫描，必然失败。
    _wait_page_content(page, timeout=15000)

    if _skip_if_no_attempts(page):
        details['status'] = 'no_attempts'
        return details

    if not _enter_quiz(page):
        # 今日已答过：页面停在“已完成 / 次数用完”终态，本就没有开始按钮。
        # 这是正常态而非故障（同日重复运行、或白天已手动答过都会命中），按已完成上报。
        if _quiz_already_done(page):
            details['status'] = 'done'
            details['reason'] = '今日已答过（页面显示已完成/次数用完）'
            logger.info('%s 今日已答过，按已完成处理', task_label)
            return details
        details['status'] = 'failed'
        details['reason'] = 'Could not enter quiz (start button not found / did not open quiz)'
        _log_quiz_diag(page, task_label)
        _dump_debug_html(page, task_label)
        return details

    _wait_quiz_ready(page, timeout=8000)
    time.sleep(0.5)

    # 读真实总题数（Vue listArr.length）作为闭环依据；取不到降级用 max_questions。
    total = _total_questions(page) or max_questions
    details['expected'] = total
    safety = total + 5  # 硬上限，防异常时死循环

    # 不再用固定 range：循环到「已答 + 已跳 == 预期」为止，确保每题都被尝试。
    guard = 0
    while (details['answered'] + details['skipped']) < total and guard < safety:
        guard += 1
        summary = _capture_quiz_summary(page)
        # 先短重试解析，吃掉“posNum 已变但 right.uuid 未填充”的竞态（偶发跳题根因）。
        correct = _wait_resolve_correct(page, timeout=8000)
        question_record = {
            'question': summary.get('question'),
            'options': summary.get('options'),
            'selected_uuid': None,
            'correct_uuid': correct['uuid'] if correct else None,
        }

        if not correct:
            # 结算/解析页：视为完成（前提是已答过题）。
            if _quiz_is_analysis(page):
                if details['answered'] > 0:
                    logger.info('%s 进入结算/解析页，判定为完成（已答 %d / 预期 %d）',
                                task_label, details['answered'], total)
                else:
                    logger.warning('%s 进入结算页但一题未答，疑似提前结束', task_label)
                details['status'] = 'done'
                return details
            # 非结算页却解析不到正确项：区分「题已加载但无法解析」与「题间过渡」。
            cq = _current_question(page)
            opts = (cq or {}).get('options') or []
            if opts:
                # 题已加载、有选项但正确项无法解析：明确记为“跳过”并打日志（绝不静默丢题）。
                idx = details['answered'] + details['skipped'] + 1
                logger.warning('%s 第 %d 题有选项但无法解析正确项，记为跳过: %s',
                               task_label, idx,
                               (summary.get('question') or '').strip().replace('\n', ' ')[:80])
                details['skipped'] += 1
                question_record['skipped'] = True
                details['questions'].append(question_record)
                pos_pre = _current_pos(page)
                _click_button_by_text(page, ['下一题', '提交', '完成'])
                _await_advance(page, pos_pre, timeout=5000)
                continue
            # 区分「活题已挂载但数据未填充」与「真·题间过渡」。
            if cq is not None:
                # 活题已挂载（curTopic 存在）但 right.uuid / answer 尚未填充：
                # 这是数据未就绪的竞态，绝不盲目前进，回到循环顶重试解析。
                logger.warning('%s 第 %d 题：活题已挂载但正确项未就绪，重试等待（不前进、不跳过）',
                               task_label, details['answered'] + details['skipped'] + 1)
                time.sleep(0.8)
                continue
            # 真·题间过渡（无任何题目数据）：推进一次再试。
            pos_pre = _current_pos(page)
            _click_button_by_text(page, ['下一题', '提交', '完成'])
            _await_advance(page, pos_pre, timeout=5000)
            time.sleep(0.5)
            continue

        # 解析到正确项 ≠ 页面此刻仍在答题页：站点有 150s 总计时，解析与点击之间
        # 页面可能已被站点切到结算页（尤其末题）。点击前再确认一次，避免对着结算页
        # 点不存在的选项（这正是末题 selected_uuid:null、reason=Could not click 的根因）。
        if _quiz_is_analysis(page):
            logger.info('%s 解析到正确项后页面已结算，收尾（已答 %d / 预期 %d）',
                        task_label, details['answered'], total)
            break

        # 点击正确选项：先重试几次（题间遮罩/未稳定可能导致偶发点击失败，重试即可恢复）。
        clicked = False
        qidx = details['answered'] + details['skipped'] + 1
        for _att in range(3):
            if _click_option(page, correct):
                clicked = True
                break
            logger.warning('%s 第 %d 题点击正确选项失败，重试(%d/3) uuid=%s',
                           task_label, qidx, _att + 1, correct['uuid'])
            time.sleep(0.5)
        if not clicked:
            # 点击失败先复检：站点总计时 / 末题偶发在 3 次重试期间把页面切到结算页，
            # 此刻“点不中”是页面已前进的假象，并非脚本真漏答。若已结算则按 quiz 结束收尾，
            # 不再伪装成“Could not click option”的失败（避免上报层误判与掩盖）。
            if _quiz_is_analysis(page):
                logger.warning('%s 第 %d 题点击未命中但页面已到结算页，按 quiz 已结束收尾（已答 %d / 预期 %d）',
                               task_label, qidx, details['answered'], total)
                break
            # 仍在答题页却点不中：如实记录（selected_uuid=None），不虚增已答，避免“假满分”。
            logger.warning('%s 第 %d 题无法点击正确选项 uuid=%s，判定失败',
                           task_label, qidx, correct['uuid'])
            question_record['selected_uuid'] = None
            details['questions'].append(question_record)
            details['status'] = 'failed'
            details['reason'] = f'Could not click option {correct["uuid"]}'
            _log_quiz_diag(page, task_label)
            _dump_debug_html(page, task_label)
            return details

        # 新页（bsanswerpro）选中后需点「确定」提交才会切题；老页此处无按钮，函数直接返回 False。
        if _click_quiz_confirm(page):
            logger.info('%s 第 %d 题已点「确定」提交', task_label, qidx)

        # 点击成功才计入已答并落记录（修复：原先在 click 校验前就 +1，导致点击失败时虚增已答）。
        question_record['selected_uuid'] = correct['uuid']
        details['questions'].append(question_record)
        details['answered'] += 1
        logger.info('%s 作答: %s | 正确uuid=%s',
                    task_label,
                    (summary.get('question') or '').strip().replace('\n', ' ')[:80],
                    correct['uuid'])

        # 前进等待（零点击）：站点真实语义（quiz.js nextQues）为“点击选项 = 提交 +
        # 自动切题”，答题页不存在“确定/确认/下一题”按钮（DOM 里的“确定”只属于
        # 提交失败弹窗）。原先在此点“确定/确认”建立在错误模型上：get_by_text 子串
        # 匹配会误点含“确定”字样的选项，把未解析的下一题以垃圾答案提交跳过
        # （08-18 与 09-07 两次事故均缺第2题、作答间隔均 ~0.51s 即此根因）。
        # 现改为纯等待：等页面自行前进（posNum 变化）或进入结算页，不做任何点击。
        pos_pre = _current_pos(page)
        if not _await_advance(page, pos_pre, timeout=5000):
            logger.warning('%s 点击选项后 5000ms 内页面未前进（pos=%s），零点击等待，交由下轮循环复检',
                           task_label, pos_pre)
        time.sleep(0.4)

    # 闭环结束：未答满预期必打警告（含跳过数），不再静默显示“完成”。
    if details['answered'] < total:
        logger.warning('%s 未答满预期题数（已答 %d / 预期 %d，跳过 %d），可能仍有跳题',
                       task_label, details['answered'], total, details['skipped'])
        details['status'] = 'done_with_skips' if details['answered'] > 0 else 'failed'
    else:
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
    _goto(page, homepage_url, timeout=15000)
    if not _click_text(page, '每日一学'):
        details['status'] = 'skipped'
        details['reason'] = '未找到 每日一学 入口，跳过（可能已下线或不在本活动页）'
        result['daily_learn'] = details
        return

    _wait_for_reload(page, timeout=8000)
    # 日历/已完成天标记可能由慢速 AJAX 渲染，给足超时；类名也可能已变更，逐级兜底。
    try:
        page.wait_for_selector('.active__done', timeout=25000)
    except Exception:
        pass

    elements = page.query_selector_all('.active__done a')
    if not elements:
        elements = page.query_selector_all('.active__done')
    # 兜底：直接扫描课程链接（类名变更时仍可按 URL 模式命中）
    if not elements:
        elements = page.query_selector_all('a[href*="activedaily"]')
    if not elements:
        elements = page.query_selector_all('a[href*="daily="]')
    if not elements:
        # 诊断：记录当前页结构，便于下次定位新选择器
        try:
            diag_url = page.url
            hrefs = [a.get_attribute('href') or '' for a in page.query_selector_all('a')]
            logger.warning('每日一学 未找到课程入口 | URL=%s | 含daily的链接=%s',
                           diag_url, [h for h in hrefs if 'daily' in h][:10])
        except Exception:
            pass
        # 追加诊断：实测点「每日一学」后落到的是文章页 /aero/detnews?uuid=...
        # —— 说明站点可能已改成「点击进入今日课程正文」，此时页面上本就没有 daily= 链接，
        #    光 dump 链接看不出该怎么拿分。把页面按钮和正文开头打出来才能判断下一步。
        try:
            page_info = page.evaluate("""() => {
                const btns = [];
                document.querySelectorAll('button, a, [role="button"], .btn').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && t.length <= 20) btns.push(t);
                });
                const body = document.body ? (document.body.innerText || '') : '';
                const marks = ['已完成', '已学习', '今日已完成', '学习完成', '已完成学习'];
                return {buttons: [...new Set(btns)].slice(0, 20),
                        bodyLen: body.length,
                        bodyHead: body.replace(/\\s+/g, ' ').slice(0, 300),
                        doneMarks: marks.filter(m => body.indexOf(m) >= 0)};
            }""")
            logger.warning('每日一学 页面诊断: %s', json.dumps(page_info, ensure_ascii=False))
        except Exception as e:
            logger.debug('每日一学 页面诊断失败: %s', e)
        details['status'] = 'skipped'
        details['reason'] = '未找到课程入口元素（active__done/daily 链接），跳过'
        result['daily_learn'] = details
        return

    # 取 daily 最大的那条作为“上一个已完成天”（兼容列表非按序渲染）
    last = None
    best_d = -1
    for el in elements:
        h = el.get_attribute('href') or ''
        m = re.search(r'[?&]daily=(\d+)', h)
        if m and int(m.group(1)) > best_d:
            best_d, last = int(m.group(1)), el
    if last is None:
        last = elements[-1]
    href = last.get_attribute('href') or ''
    if not href:
        parent = last.query_selector('a')
        href = parent.get_attribute('href') if parent else ''
    if not href:
        details['status'] = 'skipped'
        details['reason'] = '未找到课程链接 href，跳过'
        result['daily_learn'] = details
        return

    try:
        next_url, assign, cntid, daily, subTask, ptaskid = _build_next_daily_url(href, page.url)
        _goto(page, next_url, timeout=20000)
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


# ---------------------------------------------------------------------------
# 每日一炼（工间微运动 AI 体育）—— 伪造计数上报拿分
# 机制（参考用户提供的 ai_sports (1).py，已复现）：AI 运动识别完全在前端本地跑
# （MediaPipe Pose），计数结果由前端 POST 上报，服务端无条件信任 JSON 里的
# total_count/valid_count/total_duration/exercise_type。
# 服务端三道校验：
#   1) 会话 UUID 必须服务端生成 —— 打开 project-detail 页点「开始AI训练」（等价于发
#      LiveView start_training 事件），从 live_redirect 取 UUID；
#   2) CSRF token —— 从页面 meta[name=csrf-token] 读取；
#   3) 每日 10 分上限 —— 无法绕过（实测第 6~8 次提交 200 但记 0 分）。
# 集成方式：复用登录后的 Playwright 上下文（page.request 共享 cookie；LiveView 会话
# 创建走 page.evaluate 内联 WebSocket，等价于点「开始AI训练」，不引入额外依赖）。
# 经用户确认：每次做 5 项即停（5×2=10 封顶）。
# 注：本任务不进 histscore 反查/降级（其积分明细类别待首次跑完确认），状态自报。
# ---------------------------------------------------------------------------
AI_SPORTS_ACTIVITY_ID = "82456a996a3a77eb14bae7f0cda7ab1d"
AI_SPORTS_PROJECTS = {
    "push-up": "552ae1930d784ccb52cdb9c7e296b752",
    "sit-up": "680ceaa5c20c16307fb259b502c59256",
    "squat": "7b141d0ed5ec2030164c50361b1237b4",
    "side-bend": "9b0c50d60ffcff3933b2c4709fe424d5",
    "chest-expansion": "253f5ce528f75277b1e4c032e456e6db",
    "high-five": "5eb456c41372ce5ec30c1d5c56e8f86c",
    "high-knees": "ada19307efded791529f94646ab4cef0",
    "running-in-place": "457819a65465e80615cb53eb2b6b0e00",
}
AI_SPORTS_NAMES = {
    "push-up": "俯卧撑", "sit-up": "仰卧起坐", "squat": "深蹲",
    "side-bend": "体侧运动", "chest-expansion": "扩胸运动",
    "high-five": "胯下击掌", "high-knees": "高抬腿",
    "running-in-place": "原地跑",
}
AI_SPORTS_DEFAULT_COUNTS = {
    "push-up": 10, "sit-up": 10, "squat": 10, "side-bend": 10,
    "chest-expansion": 10, "high-five": 10, "high-knees": 10,
    "running-in-place": 120,
}
# 做 5 项即停（5×2=10 封顶）；原地跑按 120 步计 2 分。
AI_SPORTS_EXERCISES = ("push-up", "sit-up", "squat", "side-bend", "chest-expansion")
AI_SPORTS_TRACK_STATIC = [
    "https://www.upfitapp.com/phx-static/assets/app-b6551ca623bb22b603b7eb76388c1aac.css?vsn=d",
    "https://www.upfitapp.com/phx-static/vendor/leaflet/leaflet-c02c12fe5e21d2493070649584ca38b7.css?vsn=d",
    "https://www.upfitapp.com/phx-static/assets/app-f008476f0021bf6ee7e602410605ea56.js?vsn=d",
]
# 注意：必须是「待调用的函数」而非自执行 IIFE。
# Playwright 的 page.evaluate(expression, arg) 对已是调用表达式的字符串（形如 (fn)() ）
# 会直接求值而不传入 arg，导致 params===undefined -> "Cannot read properties of undefined (reading 'csrf')"。
_AI_SPORTS_WS_JS = r"""
async (params) => {
  const csrf = params.csrf, topic = params.topic, session = params.session,
        trackStatic = params.trackStatic, url = params.url;
  const q = new URLSearchParams();
  q.set('_csrf_token', csrf);
  trackStatic.forEach((t, i) => q.append('_track_static[' + i + ']', t));
  q.set('_mounts', '0'); q.set('_mount_attempts', '0');
  q.set('_live_referer', 'undefined'); q.set('vsn', '2.0.0');
  const ws = new WebSocket('wss://' + location.host + '/phx-live/websocket?' + q.toString());
  const deadline = Date.now() + 12000;
  return await new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (Date.now() > deadline) { clearInterval(timer); try { ws.close(); } catch(e){} reject('timeout'); }
    }, 400);
    ws.onopen = () => {
      ws.send(JSON.stringify(["4","4",topic,"phx_join",
        {url: url, params:{_csrf_token:csrf,_track_static:trackStatic,_mounts:0,_mount_attempts:0}, session: session}]));
    };
    ws.onmessage = (ev) => {
      let data; try { data = JSON.parse(ev.data); } catch(e) { return; }
      // 照原脚本：join 返回非 ok 要立刻报错。少了这一步，join 失败时只会干等到
      // 12s 超时抛 'timeout'，看不出到底是 join 被拒还是没收到会话 UUID。
      if (data[3] === 'phx_reply' && data[4] && data[4].status && data[4].status !== 'ok') {
        clearInterval(timer); try { ws.close(); } catch(e){}
        reject('phx_join failed: ' + JSON.stringify(data[4]).slice(0, 200));
        return;
      }
      if (data[3] === 'phx_reply' && data[4] && data[4].status === 'ok') {
        ws.send(JSON.stringify(["4","5",topic,"event",{type:"click",event:"start_training",value:{value:""}}]));
      }
      const s = JSON.stringify(data[4] || '');
      const m = s.match(/real-time-training\/([0-9a-f-]{36})/);
      if (m) { clearInterval(timer); try { ws.close(); } catch(e){} resolve(m[1]); }
    };
    ws.onerror = (e) => { clearInterval(timer); reject('ws_error'); };
  });
}
"""


def _ai_sports_create_session(page, exercise_type, project_id, activity_id, csrf):
    """等价于点「开始AI训练」：从 project-detail 页取 LiveView topic/session，
    通过 page.evaluate 内联 WebSocket 发 start_training 事件，拿服务端生成的会话 UUID。
    返回 UUID 字符串；失败抛 RuntimeError。"""
    url = (f"https://www.upfitapp.com/ai-sports/client/project-detail/{project_id}"
           f"?activity_id={activity_id}&exercise_type={exercise_type}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        raise RuntimeError(f"[{exercise_type}] 打开 project-detail 失败: {e}")
    try:
        # 照验证过的 ai_sports 原脚本：用正则精确取承载 data-phx-session 的 <div id="phx-...">
        # （页面存在多个 phx- 开头的 LiveView 组件 id，用 [id^="phx-"] 取第一个会拿错元素，
        #  session 为空/错误 → WS join 失败 → 全部运动 0 分）。属性值需 html.unescape。
        src = page.content()
        tag = re.search(r'<div id="(phx-[^"]+)"[^>]*data-phx-session="([^"]*)"', src)
        info = None
        if tag:
            info = {'topic': 'lv:' + tag.group(1), 'session': html.unescape(tag.group(2))}
    except Exception as e:
        raise RuntimeError(f"[{exercise_type}] 提取 LiveView 配置失败: {e}")
    if not info or not info.get('topic'):
        raise RuntimeError(f"[{exercise_type}] 未找到 LiveView 会话配置")
    try:
        uuid = page.evaluate(_AI_SPORTS_WS_JS, {
            "csrf": csrf, "topic": info['topic'], "session": info['session'],
            "url": url, "trackStatic": AI_SPORTS_TRACK_STATIC,
        })
    except Exception as e:
        raise RuntimeError(f"[{exercise_type}] LiveView 创建会话失败: {e}")
    if not uuid:
        raise RuntimeError(f"[{exercise_type}] 未收到会话 UUID")
    return uuid


def _daily_refine(page, homepage_url):
    """每日一炼：伪造 5 项运动计数上报，拿满每日 10 分上限。返回 details dict。"""
    details = {'status': 'failed', 'score': 0, 'exercises': []}
    try:
        page.goto(f"https://www.upfitapp.com/ai-sports/client?activity_id={AI_SPORTS_ACTIVITY_ID}",
                  wait_until="domcontentloaded", timeout=20000)
        # 照原脚本：正则取 csrf-token 属性值并 html.unescape（原脚本显式解码，属性值可能含 HTML 实体）。
        # 兼容两种属性顺序（name 在前 / content 在前），比原脚本单一正则更稳。
        src = page.content()
        # 照原脚本 __init__：先确认没被踢回登录页（登录态失效时活动页会跳转/渲染登录框）。
        # 少了这一步，登录失效时会拿着登录页上的 csrf 继续跑 5 项运动，全部失败且原因不明。
        cur = page.url or ''
        if 'login' in cur.lower() or '用户登录' in (src or ''):
            details['status'] = 'skipped'
            details['reason'] = f'每日一炼：登录态失效（当前页 {cur}），跳过'
            logger.warning('每日一炼：检测到登录页，登录态失效')
            return details
        m = (re.search(r'name="csrf-token"[^>]*content="([^"]*)"', src)
             or re.search(r'content="([^"]*)"[^>]*name="csrf-token"', src))
        csrf = html.unescape(m.group(1)) if m else None
        if not csrf:
            details['status'] = 'skipped'
            details['reason'] = '每日一炼：未取到 csrf-token（活动页结构变化或未开放），跳过'
            return details
    except Exception as e:
        details['status'] = 'skipped'
        details['reason'] = f'每日一炼：打开活动页失败: {e}，跳过'
        return details

    logger.info('每日一炼：活动页已打开（csrf 已取到），开始 %d 项运动上报', len(AI_SPORTS_EXERCISES))
    total_score = 0
    ok_count = 0
    for ex in AI_SPORTS_EXERCISES:
        name = AI_SPORTS_NAMES.get(ex, ex)
        rec = {'exercise': ex, 'name': name, 'status': 'failed'}
        try:
            uuid = _ai_sports_create_session(page, ex, AI_SPORTS_PROJECTS[ex],
                                            AI_SPORTS_ACTIVITY_ID, csrf)
            count = AI_SPORTS_DEFAULT_COUNTS.get(ex, 10)
            # ⚠️ Playwright 的 APIRequestContext.post() **没有 json= 参数**（那是 requests 的写法），
            # 写成 json= 会直接抛 "unexpected keyword argument 'json'"
            # → 5 项运动全部 failed、total_score=0（2026-09-20 04:05 那次运行即此）。
            # 正确写法是 data=：data 接受 dict / str / bytes，传 dict 时 Playwright 会
            # 自动序列化成 JSON 串并自动设置 Content-Type: application/json
            # （见 playwright 源码 _generated.py：data: Any | bytes | str | None）。
            # 故这里直接传 dict，不再手工 json.dumps + 手写 Content-Type。
            resp = page.request.post(
                f"https://www.upfitapp.com/ai-sports/real-time-training/{uuid}/complete",
                headers={"x-csrf-token": csrf,
                         "Referer": f"https://www.upfitapp.com/ai-sports/real-time-training/{uuid}"},
                data={"total_count": count, "valid_count": count,
                      "total_duration": 80, "exercise_type": ex},
                timeout=20000,
            )
            rec['http'] = resp.status
            if resp.status == 200:
                # 照原脚本 do_exercise()：complete 之后必须 sleep(1) 再读结果页。
                # 结果页是服务端异步生成的，读完立即取会拿到旧页/占位页 → 解析出 0 分，
                # 且会被当成「超上限记 0 分」而掩盖真实失败。
                time.sleep(1)
                try:
                    r2 = page.request.get(
                        f"https://www.upfitapp.com/ai-sports/client/training-result/{uuid}",
                        timeout=20000)
                    # 照原脚本：先剥掉 HTML 标签再匹配得分（得分被 <span> 分隔时，
                    # 直接对原始 HTML 匹配会失败 → 即使上报成功也解析成 0 分）。
                    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r2.text()))
                    # 照原脚本 get_result()：这两种文案代表结果页根本没生成/会话无效，
                    # 属失败，不能记成「0 分完成」。
                    if "资源不存在" in txt or "训练结果未加载" in txt:
                        rec['status'] = 'failed'
                        rec['error'] = '结果页异常：%s' % txt.strip()[:80]
                        logger.warning('每日一炼 [%s] 结果页无效: %s', name, txt.strip()[:120])
                    else:
                        ms = re.search(r"(\d+)\s*分\s*本次得分", txt)
                        sc = int(ms.group(1)) if ms else 0
                        rec['score'] = sc
                        # sc>0 计入；sc==0 但 200 = 超上限记 0 分（非失败）
                        rec['status'] = 'done' if sc > 0 else 'zero'
                        if sc > 0:
                            total_score += sc
                            ok_count += 1
                except Exception as e:
                    rec['status'] = 'failed'
                    rec['error'] = '读取结果页失败: %s' % e
            else:
                rec['status'] = 'failed'
        except Exception as e:
            rec['status'] = 'failed'
            rec['error'] = str(e)
        logger.info('每日一炼 [%s] status=%s http=%s score=%s err=%s',
                    name, rec.get('status'), rec.get('http'), rec.get('score'), rec.get('error'))
        details['exercises'].append(rec)
        # 照原脚本 run_full_day()：项与项之间 sleep(1.5)。原脚本是 1.5s，
        # 这里曾写成 1.0s，属与原脚本的最后一处偏差，为逐行对齐改回 1.5。
        time.sleep(1.5)

    details['score'] = total_score
    logger.info('每日一炼 汇总: total_score=%s ok=%s/%s', total_score, ok_count, len(AI_SPORTS_EXERCISES))
    if total_score >= 10:
        details['status'] = 'done'
    elif total_score > 0:
        details['status'] = 'done_with_skips'
        details['reason'] = f'每日一炼 仅拿 {total_score}/10 分（{ok_count}/{len(AI_SPORTS_EXERCISES)} 项成功）'
    else:
        details['status'] = 'failed'
        details['reason'] = '每日一炼 全部失败（0 分）'
    return details


# ---------------------------------------------------------------------------
# 前日页面比对：捕获首页任务列表区签名，与上一日（上次运行基线）比对，发现任务
# 增删/改名或较大结构改动时告警（用户要求“页面发生较大改动时提醒我”）。
# 基线存于仓库 page_snapshots/latest.json，由 workflow 每次运行后提交，供次日比对。
# ---------------------------------------------------------------------------
PAGE_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "page_snapshots")
PAGE_SNAPSHOT_LATEST = os.path.join(PAGE_SNAPSHOT_DIR, "latest.json")


# 页面监控范围（2026-09-20 用户指定）：**只看这两个容器**的内容或元素是否变化。
# .humanSociety-link  = 首页每日任务图标区（每日一学/看/练/答/走）
# .dailyActivityWarp  = 每日活动区（若在页面上不存在，会在日志里明确告警提示选择器可能写错，
#                       不会静默跳过 —— 避免"监控了个空气"却无人察觉）
PAGE_WATCH_SELECTORS = ('.humanSociety-link', '.dailyActivityWarp')

# 注意：必须是「待调用的函数」而非自执行 IIFE，否则 Playwright 不传 arg（见 _AI_SPORTS_WS_JS 注释）。
_CAPTURE_CONTAINERS_JS = r"""
(sels) => {
  const norm = s => (s || '').replace(/<script[\s\S]*?<\/script>/gi, '')
                             .replace(/<style[\s\S]*?<\/style>/gi, '')
                             .replace(/\d{1,2}:\d{2}:\d{2}/g, '')
                             .replace(/\d{4}-\d{2}-\d{2}/g, '')
                             .replace(/\s+/g, ' ').trim();
  const out = {};
  for (const sel of sels) {
    const host = document.querySelector(sel);
    if (!host) {
      out[sel] = {found: false, count: 0, items: [], html: ''};
      continue;
    }
    const items = [];
    host.querySelectorAll('*').forEach(n => {
      const txt = (n.textContent || '').replace(/\s+/g, ' ').trim();
      const g = a => (n.getAttribute ? (n.getAttribute(a) || '') : '');
      const href = g('href');
      const src = g('src') || g('data-src') || g('data-original');
      const leaf = !n.querySelector('*');
      // 只收有意义的节点：叶子文本、带链接的、带图片的
      if ((txt && leaf) || href || src) {
        items.push({
          tag: (n.tagName || '').toLowerCase(),
          cls: String(n.className || '').slice(0, 80),
          text: txt.slice(0, 120),
          href: href,
          src: src
        });
      }
    });
    out[sel] = {found: true, count: items.length, items: items,
                html: norm(host.innerHTML || '')};
  }
  return out;
}
"""


def _capture_page_signature(page):
    """捕获页面签名：**只采集 PAGE_WATCH_SELECTORS 指定的容器**。

    每个容器记录：是否找到、有意义元素列表（标签/类名/文本/链接/图片）、
    容器内部规范化 HTML 的哈希。容器之外的一切（轮播图、整页主区、倒计时等）
    一律不采集 —— 那些对运营换图/动态内容过于敏感，实测每次运行必变，纯噪音。
    """
    try:
        raw = page.evaluate(_CAPTURE_CONTAINERS_JS, list(PAGE_WATCH_SELECTORS)) or {}
    except Exception as e:
        logger.warning('capture page signature failed: %s', e)
        return None

    containers = {}
    for sel in PAGE_WATCH_SELECTORS:
        d = raw.get(sel) or {}
        html = d.get('html') or ''
        containers[sel] = {
            'found': bool(d.get('found')),
            'count': int(d.get('count') or 0),
            'items': d.get('items') or [],
            'hash': hashlib.sha256(html.encode('utf-8')).hexdigest() if html else None,
        }
    missing = [s for s in PAGE_WATCH_SELECTORS if not containers[s]['found']]
    if missing:
        found_any = any(containers[s]['found'] for s in PAGE_WATCH_SELECTORS)
        if found_any:
            # 两个活动页 DOM 外壳不同：0823 是 .humanSociety-link，0820 是 .dailyActivityWarp，
            # 各自缺另一个属正常。只要还有一个容器命中，监控就没瞎，按 info 记，不当告警。
            logger.info('页面监控：容器 %s 在当前页不存在（该页用另一套外壳），本次不参与比对',
                        ', '.join(missing))
        else:
            # 一个都没命中才是真故障：站点改名或选择器写错，必须告警。
            logger.warning('页面监控：全部容器 %s 均未找到，本次无法比对（请核对选择器）',
                           ', '.join(missing))
    return {'containers': containers}


def _load_previous_signature():
    try:
        if os.path.exists(PAGE_SNAPSHOT_LATEST):
            with open(PAGE_SNAPSHOT_LATEST, encoding='utf8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_signature_today(sig):
    try:
        os.makedirs(PAGE_SNAPSHOT_DIR, exist_ok=True)
        today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
        payload = {'date': today, 'sig': sig}
        with open(PAGE_SNAPSHOT_LATEST, 'w', encoding='utf8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(PAGE_SNAPSHOT_DIR, f"{today}.json"), 'w', encoding='utf8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('save page signature failed: %s', e)


def _item_key(x):
    """把容器内的一个元素折成可比对的键：标签|类名|文本|链接|图片。"""
    return '|'.join([str(x.get('tag') or ''), str(x.get('cls') or ''),
                     str(x.get('text') or ''), str(x.get('href') or ''),
                     str(x.get('src') or '')])


def _diff_signature(prev_sig, cur_sig):
    """纯函数：比对两份页面签名，返回 alert dict（不依赖浏览器，便于本地单测）。

    告警口径（2026-09-20 用户最终指定）：**只看 PAGE_WATCH_SELECTORS 两个容器**
    （.humanSociety-link 与 .dailyActivityWarp）的**内容或元素**是否变化。
    轮播图 / 整页主区哈希一律不采集也不告警 —— 它们对运营换图、倒计时、随机推荐
    过于敏感，实测每次运行必变，纯噪音，会淹没真正需要关注的任务变动。

    兼容：旧基线没有 containers 结构时跳过比对（本次仅记录新基线），避免改版后
    第一次运行把整份内容当「新增」误报一次。
    """
    alert = {'checked': True, 'changed': False, 'added': [], 'removed': [],
             'renamed': [], 'main_len_changed': False,
             'banner_added': [], 'banner_removed': [], 'banner_changed': False,
             'containers': {}, 'container_changed': False,
             'container_missing': [], 'container_appeared': []}
    prev_c = (prev_sig or {}).get('containers')
    cur_c = (cur_sig or {}).get('containers') or {}
    if not prev_c:
        return alert

    for sel in PAGE_WATCH_SELECTORS:
        p, c = prev_c.get(sel), cur_c.get(sel)
        if p is None or c is None:
            continue
        detail = {'found_prev': bool(p.get('found')), 'found_cur': bool(c.get('found')),
                  'added': [], 'removed': [], 'hash_changed': False,
                  'count_changed': False}
        if detail['found_prev'] != detail['found_cur']:
            if detail['found_cur']:
                alert['container_appeared'].append(sel)
            else:
                alert['container_missing'].append(sel)
        if detail['found_prev'] and detail['found_cur']:
            pk = [_item_key(x) for x in (p.get('items') or [])]
            ck = [_item_key(x) for x in (c.get('items') or [])]
            ps, cs = set(pk), set(ck)
            for k in sorted(cs - ps):
                detail['added'].append(k)
                alert['added'].append({'container': sel, 'item': k})
            for k in sorted(ps - cs):
                detail['removed'].append(k)
                alert['removed'].append({'container': sel, 'item': k})
            detail['hash_changed'] = (p.get('hash') != c.get('hash'))
            detail['count_changed'] = (p.get('count') != c.get('count'))
        alert['containers'][sel] = detail
        if detail['added'] or detail['removed'] or detail['hash_changed']:
            alert['container_changed'] = True

    alert['changed'] = bool(alert['added'] or alert['removed']
                            or alert['container_missing']
                            or alert['container_appeared']
                            or alert['container_changed'])
    return alert


def _compare_page_with_previous(page):
    """捕获今日首页签名并与上一日基线比对，返回 alert dict（告警口径见 _diff_signature）。"""
    sig = _capture_page_signature(page)
    if not sig:
        return None
    prev = _load_previous_signature()
    if prev and isinstance(prev, dict) and 'sig' in prev:
        alert = _diff_signature(prev['sig'], sig)
        if alert['changed']:
            logger.warning('页面监控区发生变动：新增元素=%s 消失元素=%s 容器消失=%s 容器新增=%s',
                           alert['added'], alert['removed'],
                           alert['container_missing'], alert['container_appeared'])
        else:
            logger.info('页面监控区无变化（%s）',
                        ', '.join('%s:%s' % (k, '已找到' if v.get('found_cur') else '未找到')
                                  for k, v in alert.get('containers', {}).items())
                        or '无容器基线')
    else:
        alert = {'checked': True, 'changed': False}
    _save_signature_today(sig)
    return alert


def _daily_view(page, result, homepage_url):
    details = {'status': 'failed'}
    _goto(page, homepage_url, timeout=15000)
    if not _click_text(page, '每日一看'):
        details['status'] = 'skipped'
        details['reason'] = '未找到 每日一看 入口，跳过（可能已下线）'
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
# 经验证映射（活跃 Web 任务）：
#   限时活动I + 每日一答      -> daily_answer
#   限时活动III（+ 任意专题名）-> daily_learn（每日一学）
#   AI运动会（+2/活动, 含类别或任务名）-> daily_refine（每日一炼）
#   （每日一看 daily_view=文章阅读 / 每日一练 daily_practice=练兵比武 入口不稳定，
#    未纳入 histscore 映射，执行时找不到即跳过）
# 注意：得分不固定，故以「类别+任务名+今日日期」判定，
# 不以具体分数判定。时间为服务器北京时间，需用北京时间比对「今日」。
# ---------------------------------------------------------------------------

def _histscore_match(task_key):
    return {
        'daily_answer': ('限时活动I', '每日一答'),
        'daily_learn': ('限时活动III', None),  # 专题名会变，只按类别判定
        'daily_refine': ('AI运动会', None),    # 每日一炼：积分明细类别/任务名含「AI运动会」，+2/活动
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
        _goto(page, histscore_url, timeout=20000)
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
    if task_key == 'daily_refine':
        # 每日一炼：积分明细里「AI运动会」可能落在类别(span0)或任务名(span1)，兜底双列 contains 匹配。
        for row in rows:
            if ('AI运动会' in (row.get('cat') or '') or 'AI运动会' in (row.get('name') or '')) \
               and (row.get('time') or '').startswith(today_mmdd):
                return {'done': True, 'score': row.get('score'), 'time': row.get('time'), 'name': row.get('name')}
        return {'done': False}
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
    返回 {motion, reach, total, has_both}（motion / reach 为当日同名标签得分**之和**）。

    ⚠️ 同一天可能出现**多个同名标签**（如多次运动 / 多次达标记录），必须**求和**
    而非只取第一条，否则会少算（实测曾把 07-24 误判为 +8 而非正确的 +10）。
    """
    motion_sum = 0
    reach_sum = 0
    motion_seen = False
    reach_seen = False
    for row in rows:
        if not (row.get('time') or '').startswith(today_mmdd):
            continue
        nm = row.get('name', '') or ''
        ct = row.get('cat', '') or ''
        sc = _parse_score(row.get('score'))
        if sc is None:
            continue
        if '每日运动' in nm or '每日运动' in ct:
            motion_sum += sc
            motion_seen = True
        elif '步数达标' in nm or '步数达标' in ct:
            reach_sum += sc
            reach_seen = True
    return {
        'motion': motion_sum if motion_seen else None,
        'reach': reach_sum if reach_seen else None,
        'total': motion_sum + reach_sum,
        'has_both': motion_seen and reach_seen,
    }


def _histscore_today_gain(page, homepage_url, today_mmdd):
    """用积分明细账本核算该账号今日净增总分（跨所有活动页的权威账本）。

    与逐网址读首页 `.topIntegral-number` 不同：histscore 是同一账号的单一账本，
    两个活动页（TARGET_URL / TARGET_URL_2）的得分都落在同一份记录里，且不受
    第二网址首页积分元素读取超时（Page.wait_for_selector Timeout 10000ms）影响。
    返回今日(mm-dd)全部得分项之和（int，含正负得分为净增）；读取/解析失败返回 None。
    """
    try:
        rows = _read_histscore_rows(page, homepage_url)
        if not rows:
            return None
        total = 0
        for row in rows:
            if not (row.get('time') or '').startswith(today_mmdd):
                continue
            sc = _parse_score(row.get('score'))
            if isinstance(sc, int):
                total += sc
        return total
    except Exception as e:
        logger.warning('histscore 今日净增核算失败: %s', e)
        return None


def _verify_all_tasks(page, result, homepage_url=None, allow_upgrade: bool = True):
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
        for key in ('daily_learn', 'daily_answer', 'daily_refine'):
            prev = result['tasks'].get(key)
            if not isinstance(prev, dict):
                continue
            verdict = _verify_task_via_histscore(rows, key, today_mmdd)
            prev['histscore_done'] = bool(verdict and verdict.get('done'))
            if verdict and verdict.get('done'):
                prev['histscore_score'] = verdict.get('score')
                prev['histscore_time'] = verdict.get('time')
                inpage = prev.get('status')
                expected = prev.get('expected') or 0
                answered = prev.get('answered') if 'answered' in prev else None
                counted = bool(expected) and answered is not None
                if inpage == 'done':
                    # 页面已报完成，histscore 仅交叉确认
                    prev['verified_by'] = 'histscore'
                elif counted and answered < expected:
                    # ★ 关键修正：页面真实计过数且答不满（done_with_skips/failed）。
                    # histscore 有积分 ≠ 全部答完（部分作答也得分），绝不升级为 done。
                    prev['verified_by'] = 'histscore_points_only'
                    prev['reason'] = ('histscore shows %s points @ %s, but in-page answered %s/%s '
                                      '(status=%s); NOT upgraded. orig: %s') % (
                        verdict.get('score'), verdict.get('time'),
                        answered, expected, inpage, prev.get('reason'))
                    logger.warning('Task %s kept as %s: in-page %s/%s, histscore only proves points',
                                   key, inpage, answered, expected)
                else:
                    # ★ 关键护栏：积分明细无法区分分数来自哪个活动页。
                    #   TARGET_URL 与 TARGET_URL_2 是不同活动，若在本页「执行失败」时
                    #   用 histscore 升级成 done，等于拿另一个活动页的分数冒充本页完成
                    #   —— 0820「每日一答」长期显示成功却从未答题，就是这个假信号。
                    #
                    #   例外：daily_refine（每日一炼 / AI运动会）是**全局唯一入口**，
                    #   不依赖活动页、不存在「哪个 URL 的分数」歧义，分数可明确归因。
                    #   它在第二个 URL 上重跑时必然 0 分（今日 10 分上限已满），
                    #   若不允许升级就会凭空多出一个红色失败项，属于误报。
                    if not allow_upgrade and key != 'daily_refine':
                        prev['verified_by'] = 'histscore_no_upgrade'
                        prev['reason'] = ('histscore has %s @ %s today, but points cannot be '
                                          'attributed to this activity page; NOT upgraded. '
                                          'orig(%s): %s') % (
                            verdict.get('score'), verdict.get('time'),
                            inpage, prev.get('reason'))
                        logger.warning('Task %s NOT upgraded (score source ambiguous on '
                                       'non-primary URL); in-page status=%s', key, inpage)
                        continue
                    # 页面状态不可信（如 daily_learn 选择器漏判，无 expected）→ histscore 为权威，升级 done
                    orig = prev.get('reason')
                    prev['upgraded_from'] = inpage
                    prev['status'] = 'done'
                    prev['verified_by'] = 'histscore'
                    prev['reason'] = 'histscore confirms completed today (%s @ %s); original: %s' % (
                        verdict.get('score'), verdict.get('time'), orig)
                    logger.info('Task %s upgraded to done via histscore (%s @ %s) [originally %s]',
                                key, verdict.get('score'), verdict.get('time'), inpage)
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
        for key in ('daily_learn', 'daily_answer', 'daily_refine'):
            verdict = _verify_task_via_histscore(rows, key, today_mmdd)
            if verdict and verdict.get('done'):
                plan[key] = verdict
                logger.info('preflight: %s 今日已得分 (%s @ %s) -> 跳过执行',
                            key, verdict.get('score'), verdict.get('time'))
        return plan
    except Exception as e:
        logger.warning('preflight histscore read failed: %s', e)
        return {}


def _random_sleep(lo: int, hi: int, label: str) -> None:
    """随机等待 lo~hi 秒（含端点），用于错峰、把对 upfitapp 的请求打散。每次独立随机。"""
    secs = random.randint(lo, hi)
    logger.info("随机等待 %ds：%s", secs, label)
    time.sleep(secs)


def run_account(account, url, timeout: int = 30, walk_once: bool = True, reuse_page=None,
                compare_snapshot: bool = True, preflight: bool = True, is_last_url: bool = False):
    result = {
        'user': account.get('name') or account.get('username'),
        'status': 'failed',
        'tasks': {},
    }
    username = account.get('username')
    password = account.get('password')

    try:
        own_browser = None
        if reuse_page is not None:
            page = reuse_page
        else:
            own_browser = Browser()
            page = own_browser.start()
        try:
            _goto(page, url, timeout=timeout * 1000)
            # 登录态检测：页面无登录表单 = 已登录（复用会话/同域 cookie 生效），跳过登录直接进任务序列。
            # 误判（如页面改版）时后续任务会因找不到入口如实暴露为 skipped/failed，不会静默错。
            already_logged_in = False
            try:
                page.wait_for_selector('form[onsubmit*="isSubmit"]', state='attached', timeout=8000)
            except Exception:
                already_logged_in = True
            if already_logged_in:
                _wait_for_reload(page, timeout=8000)
                logger.info('已登录会话生效（未检测到登录表单），跳过登录: %s', url)
                result['tasks']['login'] = {'status': 'done', 'reused': True}
            else:
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

            _goto(page, url, timeout=timeout * 1000)
            _wait_for_reload(page, timeout=10000)
            result['initial_points'] = _read_points(page)
            if result['initial_points'] is None:
                logger.warning('Could not read initial points after login')

            result['tasks']['login'] = {'status': 'done'}

            # 首页任务列表区「与前一日比对」：捕获今日签名、与仓库基线比对、告警页面较大改动；
            # 同时把今日签名存为基线（供次日比对）。基线由 workflow 提交 page_snapshots/ 持久化。
            # 仅首个 URL 轮比对并存基线：基线 latest.json 是单份的，若第二个活动页也覆盖写，
            # 每天两轮都会拿「上一页的基线」比对而误告警页面变动。
            if compare_snapshot:
                try:
                    result['page_change_alert'] = _compare_page_with_previous(page)
                except Exception as e:
                    logger.warning('页面比对失败（不影响主流程）：%s', e)
                    result['page_change_alert'] = None

            # 登录后预检：对照积分明细，今日已满分的任务直接跳过，避免重复执行 / 浪费答题时限。
            # ⚠️ 仅首个 URL 轮启用（preflight）：两个活动页的任务是「不同活动」，积分明细无法区分
            #    分数来自哪一页，若跨 URL 沿用预检，第二个 URL 的任务会被整体误跳过。
            #    （用户 2026-09-20 明确：TARGET_URL 与 TARGET_URL_2 任务不同，不要自动跳过；
            #     同一 URL 内已答满分的才跳过。）
            today_mmdd = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%m-%d")
            skip_plan = _preflight_skip_plan(page, today_mmdd, url) if preflight else {}

            def _mark_skipped(key, verdict):
                result['tasks'][key] = {
                    'status': 'done',
                    'skipped': True,
                    'verified_by': 'histscore_preflight',
                    'histscore_score': verdict.get('score'),
                    'histscore_time': verdict.get('time'),
                    'reason': 'histscore shows score for today; task skipped (already full)'
                }

            # 每日一学（活动III 专题，按类别判定；选择器曾漏判，靠 histscore 兜底）
            if 'daily_learn' in skip_plan:
                _mark_skipped('daily_learn', skip_plan['daily_learn'])
            else:
                _daily_learn(page, result['tasks'], url)
            _random_sleep(60, 180, "积分任务间随机等待（每日一学后）")

            # 每日一看（文章阅读）：入口可能已下线，找不到则跳过（skip），不阻断其余任务。
            if 'daily_view' in skip_plan:
                _mark_skipped('daily_view', skip_plan['daily_view'])
            else:
                _daily_view(page, result['tasks'], url)
            _random_sleep(60, 180, "积分任务间随机等待（每日一看后）")

            # 每日一炼（工间微运动 AI 体育）—— 2026-09 新增；伪造计数拿满 10 分上限。
            # 积分明细类别为「AI运动会」(+2/活动, 5 活动=10)，已纳入 histscore 反查/预检跳过。
            if 'daily_refine' in skip_plan:
                _mark_skipped('daily_refine', skip_plan['daily_refine'])
            else:
                result['tasks']['daily_refine'] = _daily_refine(page, url)
            _random_sleep(60, 180, "积分任务间随机等待（每日一炼后）")

            # 每日一练（练兵比武）：入口可能已下线，找不到则跳过（skip_if_missing），不阻断其余任务。
            if 'daily_practice' in skip_plan:
                _mark_skipped('daily_practice', skip_plan['daily_practice'])
            else:
                result['tasks']['daily_practice'] = _run_quiz(page, '每日一练', 5, url, skip_if_missing=True)
            _random_sleep(60, 180, "积分任务间随机等待（每日一练后）")

            # 每日一答（限时活动I）
            if 'daily_answer' in skip_plan:
                _mark_skipped('daily_answer', skip_plan['daily_answer'])
            else:
                result['tasks']['daily_answer'] = _run_quiz(page, '每日一答', 5, url, skip_if_missing=True)
            _random_sleep(60, 180, "积分任务间随机等待（每日一答后）")

            # 用积分明细页交叉校验当前活跃任务今日是否完成（权威信号：修复每日一学假阴性/假阳性）
            # allow_upgrade=preflight：只有首个 URL（preflight 开启）才允许用积分明细
            # 把「执行失败」升级为 done；第二个 URL 的分数来源不可归因，禁止升级。
            _verify_all_tasks(page, result, url, allow_upgrade=preflight)

            # 每日一走：独立于 Web 答题体系，走微信小程序 liteapp openId 接口。
            # 执行顺序：① 先 run_walk 执行步数写入；② 再读 histscore 计算「每日运动+步数达标」得分汇总。
            # 写入闸门 = usrreg 同 PHPSESSID 会话，不依赖 enc/iv/key / decrypt（维护态不阻断写入）。
            # 仅当账号配置了 openid 才执行；否则标 skipped。
            walk_result = {}
            if not walk_once:
                # 每日一走走独立 liteapp 接口、与答题主页无关：每账号每天只执行一次（首个 URL）。
                walk_result = {
                    "status": "skipped",
                    "reason": "每日一走 已在首个 URL 执行，本轮跳过",
                }
            elif account.get("openid"):
                logger.info("账号 %s 配置了 openid，执行每日一走（先写步数，后读 histscore 汇分）",
                            account.get("name") or account.get("username"))
                try:
                    wk = run_walk(account)
                except Exception as e:
                    logger.exception("每日一走 run_walk 异常：%s", e)
                    wk = {"status": "failed", "reason": f"run_walk 异常：{e}"}
                # ② 后读 histscore 汇分：仅当本次确实写入成功（done）才读，避免 skipped/failed
                #    仍渲染出「今日得分汇总 +X」造成误导（得分反映当天已有值，非本次写入产生）。
                if wk.get("status") == "done":
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
            _random_sleep(60, 180, "积分任务间随机等待（每日一走后）")

            _goto(page, url, timeout=timeout * 1000)
            _wait_for_reload(page, timeout=10000)
            final_points = _read_points(page)
            if final_points is None:
                logger.warning('Could not read final points after returning to homepage')
            result['final_points'] = final_points

            # 根治：第二网址首页 `.topIntegral-number` 读取超时导致积分变「未知」、总分漏算。
            # 改用 histscore 单一账本核算该账号今日净增（跨两网址、不依赖首页元素）。
            # 仅在每账号最后一个网址读取一次即可（账本已含前面网址的贡献）。
            if is_last_url:
                try:
                    result['histscore_today_gain'] = _histscore_today_gain(page, url, today_mmdd)
                except Exception as e:
                    logger.warning('记录 histscore 今日净增失败: %s', e)

            result['status'] = 'success'
        finally:
            if own_browser is not None:
                own_browser.close()

    except Exception as e:
        tb = traceback.format_exc()
        logger.exception('Error during account workflow for %s', account.get('name') or account.get('username'))
        result['error'] = str(e)
        result['traceback'] = tb

    return result
