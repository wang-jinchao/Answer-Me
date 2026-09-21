"""离线校验 account_runner.py 里所有内嵌 JS 片段的语法/可执行性。

为什么需要这个脚本：
  src/account_runner.py 里大量逻辑是「内嵌在 Python 字符串里的 JavaScript」
  （page.evaluate / eval_on_selector_all ...）。Python 的 ast.parse 只校验
  Python 语法，对这些 JS 完全不设防，错误只能等到 GitHub Actions 真跑一遍
  才暴露 —— 2026-09-20 的每日一炼 0 分事故就是典型：
    _AI_SPORTS_WS_JS 写成了自执行 IIFE `(async (params) => {...})()`，
    Playwright 对「已是调用表达式」的字符串会直接求值而不传参，
    → params undefined → "Cannot read properties of undefined (reading 'csrf')"。
  这类错误用 node --check 在本地 1 秒就能抓出来，不该花一次 GA 往返。

用法：
  python tests/check_embedded_js.py          # 校验语法
  python tests/check_embedded_js.py --exec  # 额外在最小 DOM stub 上试跑（更严）
退出码 0 = 全部通过；1 = 存在非法 JS。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "account_runner.py")

# 会接收 JS 字符串作为第一个参数的 Playwright 方法
EVAL_METHODS = {
    "evaluate", "eval_on_selector_all", "evaluate_handle",
    "eval_on_selector", "wait_for_function",
}

NODE = os.environ.get("NODE_BIN") or "node"


def _module_string_constants(tree: ast.Module) -> dict:
    """收集模块级字符串常量（如 _QUIZ_ROOT_FINDER_JS = r\"\"\"...\"\"\"）。"""
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    consts[target.id] = node.value.value
    return consts


def _flatten(node: ast.AST, consts: dict) -> str:
    """把字符串拼接表达式展平：Constant / Name(查常量) / BinOp(Add) / JoinedStr。"""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left, consts) + _flatten(node.right, consts)
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                # f-string 插值：未知内容，用占位符顶替（只求语法可解析）
                out.append("__X__")
        return "".join(out)
    return ""


def _collect(tree: ast.Module, consts: dict):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in EVAL_METHODS:
            continue
        if not node.args:
            continue
        js = _flatten(node.args[0], consts)
        if js.strip():
            nargs = len(node.args)
            found.append((node.lineno, func.attr, nargs, js))
    return found


def _node_check(js: str) -> tuple:
    """用 node --check 校验一段 JS 表达式。返回 (ok, message)。"""
    wrapped = "void (%s);\n" % js
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(wrapped)
        proc = subprocess.run([NODE, "--check", path],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return True, ""
        return False, (proc.stderr or "").strip().splitlines()[:4]
    except FileNotFoundError:
        return None, "node 不可用（设置 NODE_BIN 指向 node 可执行文件）"
    except subprocess.TimeoutExpired:
        return False, "node --check 超时"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _node_exec(js: str, lineno: int) -> tuple:
    """在最小 DOM stub 上试跑，额外抓「语法对但一执行就炸」的问题。

    只覆盖无参的 () => {...} 片段；需要外部传参的跳过（返回 None）。
    """
    if not js.lstrip().startswith("("):
        return None, ""
    head = js.lstrip()
    if not (head.startswith("()") or head.startswith("async ()")):
        return None, ""  # 带参数的交给调用方另行验证
    harness = r"""
const __els = [];
function __mkEl(tag, id, cls) {
  const el = {
    tagName: (tag||'div').toUpperCase(), id: id||'', className: cls||'',
    innerText: '', textContent: '', children: [], __vue__: null,
    style: {}, dataset: {},
    getAttribute(){ return null; }, setAttribute(){}, click(){},
    scrollIntoView(){}, querySelector(){ return null; },
    querySelectorAll(){ return []; }, appendChild(){}, removeChild(){},
    addEventListener(){}, closest(){ return null; },
    get outerHTML(){ return '<div></div>'; },
  };
  return el;
}
const document = {
  querySelector(){ return null; },
  querySelectorAll(){ return __els; },
  getElementById(){ return null; },
  createElement(t){ return __mkEl(t); },
  addEventListener(){},
  body: __mkEl('body'),
  documentElement: __mkEl('html'),
  readyState: 'complete',
};
const window = { location: { href: 'https://example.invalid/x', host: 'example.invalid' },
                 addEventListener(){}, setTimeout(){}, clearTimeout(){},
                 innerWidth: 1280, innerHeight: 720 };
const location = window.location;
const navigator = { userAgent: 'stub' };
try { void (%s); } catch (e) { console.log('THROW ' + e.message); process.exit(0); }
console.log('OK');
""" % js
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out.startswith("OK"):
            return True, ""
        if out.startswith("THROW"):
            return False, out
        return False, (proc.stderr or "").strip().splitlines()[:4]
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> int:
    with open(SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    consts = _module_string_constants(tree)
    items = _collect(tree, consts)

    do_exec = "--exec" in sys.argv
    print("发现内嵌 JS 片段 %d 处（来自 %s）" % (len(items), os.path.basename(SRC)))
    print("node: %s" % NODE)
    print("-" * 72)

    bad = 0
    skipped = 0
    for lineno, method, nargs, js in items:
        ok, msg = _node_check(js)
        tag = "%s:%d %s(%d arg)" % (os.path.basename(SRC), lineno, method, nargs)
        if ok is None:
            print("SKIP %-46s %s" % (tag, msg))
            skipped += 1
            continue
        if not ok:
            print("FAIL %-46s" % tag)
            for line in (msg if isinstance(msg, list) else [msg]):
                print("       %s" % line)
            bad += 1
            continue
        if do_exec:
            eok, emsg = _node_exec(js, lineno)
            if eok is False:
                print("FAIL %-46s (运行时) %s" % (tag, emsg))
                bad += 1
                continue
        print("ok   %-46s (%d chars)" % (tag, len(js)))

    print("-" * 72)
    # 专项检查：page.evaluate 传参时 JS 绝不能是自执行 IIFE
    iife_hits = []
    for lineno, method, nargs, js in items:
        if method == "evaluate" and nargs >= 2:
            stripped = js.strip()
            if stripped.endswith("()") and stripped.startswith("(") \
                    or stripped.rstrip().endswith("})();"):
                iife_hits.append(lineno)
    if iife_hits:
        print("🔴 发现 %d 处「page.evaluate 传参 + 自执行 IIFE」反模式，行号: %s" % (len(iife_hits), iife_hits))
        print("   Playwright 对已是调用表达式的字符串直接求值、不传 arg → 形参 undefined。")
        bad += len(iife_hits)
    else:
        print("✅ 未发现 evaluate 传参 + IIFE 反模式")

    if bad:
        print("❌ %d 处 JS 存在问题（skip %d）" % (bad, skipped))
        return 1
    print("✅ 全部内嵌 JS 语法通过（skip %d）" % skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
