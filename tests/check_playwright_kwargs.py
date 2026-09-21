#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 Playwright API 参数校验（PWK001 / PWK002）。

背景（2026-09-20 教训）：
  排查每日一炼 0 分时，我用 `inspect.signature` 确认了「post 没有 json= 参数」就下结论，
  却**没读参数注解和 docstring**，于是把话说成 "data 只能传 str/bytes" —— 实际上
  `data: Any | bytes | str | None`，传 dict 时 Playwright 会自动序列化并补 Content-Type。
  结论：**验证 API 不能只看参数名在不在签名里，必须连注解 + 文档一起看。**

本脚本把"参数名必须在真实签名中"这条做成通用门禁（覆盖 src/ 下全部 Playwright 调用，
而不是像 check_playwright_api.py 那样只认得 4 个 requests 写法），并额外校验枚举取值。

规则
  PWK001  关键字参数不在任何候选 Playwright 类的真实签名中
          （典型：requests 的 json= / files= / verify= / allow_redirects=）
  PWK002  枚举型参数取值非法（wait_until / state）

用法
  python tests/check_playwright_kwargs.py            # 扫 src/
  python tests/check_playwright_kwargs.py --selftest # 先自证规则有效
"""
import ast
import inspect
import os
import sys

# 接收者根节点白名单：只对确定是 Playwright 对象的调用做校验，避免误报
ALLOWED_ROOTS = {
    "page", "frame", "context", "browser", "locator", "element", "elements",
    "elem", "elems", "node", "nodes", "e", "el", "item", "items", "link",
    "links", "btn", "button", "buttons", "response", "resp", "r2", "request",
    "api", "handle", "handles", "self", "loc", "locs",
}

# 枚举型参数及其合法取值（取自 playwright 文档）
ENUMS = {
    "wait_until": {"commit", "domcontentloaded", "load", "networkidle"},
    "state": {"attached", "detached", "hidden", "visible"},
}


def _playwright_classes():
    try:
        from playwright.sync_api import (APIRequest, APIRequestContext, APIResponse,
                                         Browser, BrowserContext, Dialog, Download,
                                         ElementHandle, FileChooser, Frame, Keyboard,
                                         Locator, Mouse, Page, Request, Response,
                                         WebSocket, Worker)
    except Exception:
        return []
    return [Page, Frame, Locator, ElementHandle, BrowserContext, Browser,
            APIRequestContext, APIResponse, APIRequest, Response, Request,
            Dialog, Download, FileChooser, Keyboard, Mouse, WebSocket, Worker]


CLASSES = _playwright_classes()


def _root_name(node):
    """取接收者最左侧的名字：page.request -> page"""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _allowed_kwargs(meth):
    """该方法在任意候选类签名中出现过的全部参数名"""
    allowed = set()
    for cls in CLASSES:
        fn = getattr(cls, meth, None)
        if fn is None:
            continue
        try:
            allowed.update(inspect.signature(fn).parameters.keys())
        except (TypeError, ValueError):
            continue
    return allowed


def scan_source(text, filename="<src>"):
    """返回 [(规则号, 文件名, 行号, 说明)]"""
    issues = []
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError as e:
        return [("PWK000", filename, getattr(e, "lineno", 0), "语法错误: %s" % e)]
    if not CLASSES:
        # 用独立规则号 PWKENV 区分：这不是代码里 API 用错，而是**跑校验的环境缺依赖**。
        # 曾在本机用没装 playwright 的解释器跑，12 条全报 PWK000，被误读成「项目有 12 处参数误用」。
        return [("PWKENV", filename, 0,
                 "未安装 playwright，无法比对真实签名；请 pip install playwright 后重跑"
                 "（本脚本必须依赖真实签名，不做无依赖降级，避免静默失去覆盖）")]

    cache = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute):
            continue
        root = _root_name(f.value)
        if root not in ALLOWED_ROOTS:
            continue
        meth = f.attr
        if meth not in cache:
            cache[meth] = _allowed_kwargs(meth)
        allowed = cache[meth]
        if not allowed:  # 不是任何 Playwright 类的方法，跳过（防误报）
            continue
        for kw in node.keywords:
            if kw.arg is None:  # **kwargs 展开，跳过
                continue
            if kw.arg not in allowed:
                issues.append(("PWK001", filename, node.lineno,
                               "%s(...) 的参数 '%s' 不在 Playwright 真实签名中"
                               % (meth, kw.arg)))
            if kw.arg in ENUMS and isinstance(kw.value, ast.Constant):
                val = kw.value.value
                if val not in ENUMS[kw.arg]:
                    issues.append(("PWK002", filename, node.lineno,
                                   "%s=%r 非法，允许: %s"
                                   % (kw.arg, val, ",".join(sorted(ENUMS[kw.arg])))))
    return issues


SELFTEST = [
    # (应命中?, 代码, 预期规则)
    (True, 'page.request.post(u, json={"a": 1})', "PWK001"),
    (True, 'page.request.post(u, files={"f": b"x"})', "PWK001"),
    (True, 'page.request.get(u, verify=False)', "PWK001"),
    (True, 'page.request.get(u, allow_redirects=True)', "PWK001"),
    (True, 'page.goto(u, wait_until="domcontentload")', "PWK002"),
    (True, 'locator.wait_for(state="visibles")', "PWK002"),
    (False, 'page.request.post(u, data={"a": 1}, headers=h, timeout=20000)', None),
    (False, 'page.goto(u, wait_until="domcontentloaded", timeout=20000)', None),
    (False, 'locator.first.click(timeout=5000)', None),
    (False, 'page.wait_for_selector(s, state="attached", timeout=3000)', None),
    (False, 'self.browser.new_context(user_agent=UA, viewport=None)', None),
    (False, 'resp.status', None),  # 非调用，不应命中
]


def selftest():
    if not CLASSES:
        # 环境缺依赖时直接说清楚，避免 12 条全报 PWK000 被误读成代码有问题。
        print("❌ 当前解释器未安装 playwright（import playwright 失败），无法自检。")
        print("   本脚本必须依赖 playwright 的真实签名，不做无依赖降级。")
        print("   请先 pip install playwright，再重跑 --selftest。")
        return 1
    print("自测规则有效性（BAD 必须命中 / GOOD 必须不命中）")
    print("-" * 72)
    ok = 0
    for should_hit, code, rule in SELFTEST:
        res = scan_source(code, filename="<selftest>")
        hit = bool(res)
        good = (hit == should_hit) and (not should_hit or res[0][0] == rule)
        ok += 1 if good else 0
        print("%s %-5s %-58s -> hits=%d %s"
              % ("ok  " if good else "FAIL", "BAD" if should_hit else "GOOD",
                 code[:58], len(res), ("(" + res[0][0] + ")") if res else ""))
    print("-" * 72)
    print("%s 自测通过 %d/%d 条" % ("✅" if ok == len(SELFTEST) else "❌", ok, len(SELFTEST)))
    return 0 if ok == len(SELFTEST) else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()

    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    src_dir = os.path.normpath(src_dir)
    print("扫描 %s 的 Playwright API 调用（规则 PWK001, PWK002）" % src_dir)
    print("-" * 72)
    all_issues = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(src_dir, fn)
        issues = scan_source(open(path, encoding="utf-8").read(), filename=fn)
        all_issues.extend(issues)

    if all_issues:
        for rule, f, line, msg in all_issues:
            print("❌ %s %s:%s  %s" % (rule, f, line, msg))
        print("-" * 72)
        print("发现 %d 处问题" % len(all_issues))
        return 1
    print("✅ 未发现 Playwright 参数误用（参数名与真实签名一致，枚举取值合法）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
