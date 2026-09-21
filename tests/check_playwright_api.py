"""AST 层面检测 Playwright API 误用（纯静态、无网络、秒级）。

为什么需要：Playwright 与 requests 的 API 长得像但**参数不同**，从 requests 脚本往
Playwright 移植时极易照抄踩坑，而且这类错误**运行时才炸**，只能靠一次完整 GA 往返暴露。

已发生的真实事故（2026-09-20 04:05 那次运行）：
    resp = page.request.post(url, headers=..., json={...})
    → TypeError: APIRequestContext.post() got an unexpected keyword argument 'json'
    → 每日一炼 5 项运动全部 failed、total_score=0。
  requests.post 有 json=；Playwright 的 APIRequestContext.* **没有**，必须用
  data=json.dumps(...) + 手动带 Content-Type: application/json。

本脚本用 ast 静态扫描，不 import 被检模块（避免 side effect），因此零依赖、秒级完成。
用法：python tests/check_playwright_api.py
退出码 0 = 无问题；1 = 发现误用。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "src")

# 规则表：每条规则描述一种「requests 写法照抄到 Playwright」的误用。
#   methods : 触发的方法名
#   bad_kw  : 禁止出现的关键字参数
#   hint    : 改法
RULES = [
    {
        "id": "PW001",
        "methods": {"post", "put", "patch", "delete", "get", "fetch", "head"},
        "bad_kw": "json",
        "desc": "APIRequestContext.* 不接受 json=（那是 requests 的参数）",
        "hint": "改用 data=json.dumps(payload)，并在 headers 里带 'Content-Type': 'application/json'",
    },
    {
        "id": "PW002",
        "methods": {"post", "put", "patch"},
        "bad_kw": "files",
        "desc": "APIRequestContext.* 不接受 files=（那是 requests 的参数）",
        "hint": "改用 multipart={...}",
    },
    {
        "id": "PW003",
        "methods": {"post", "put", "patch", "delete", "get", "fetch", "head"},
        "bad_kw": "verify",
        "desc": "APIRequestContext.* 不接受 verify=（那是 requests 的参数）",
        "hint": "改用 ignore_https_errors=True",
    },
    {
        "id": "PW004",
        "methods": {"post", "put", "patch", "delete", "get", "fetch", "head"},
        "bad_kw": "allow_redirects",
        "desc": "APIRequestContext.* 不接受 allow_redirects=（那是 requests 的参数）",
        "hint": "改用 max_redirects=<n>",
    },
]


def _iter_py_files():
    for name in sorted(os.listdir(SRC_DIR)):
        if name.endswith(".py"):
            yield os.path.join(SRC_DIR, name)


def _scan(tree: ast.AST, path: str):
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        method = func.attr
        for rule in RULES:
            if method not in rule["methods"]:
                continue
            for kw in node.keywords:
                if kw.arg == rule["bad_kw"]:
                    hits.append({
                        "file": os.path.basename(path),
                        "line": node.lineno,
                        "rule": rule["id"],
                        "method": method,
                        "kw": kw.arg,
                        "desc": rule["desc"],
                        "hint": rule["hint"],
                    })
    return hits


# 自证用例：确保规则本身真的能抓到（防止规则写了但形同虚设）。
# BAD 必须被 _scan 命中，GOOD 必须不命中；任一条不符即视为门禁失效。
SELFTEST_CASES = [
    ("BAD", "resp = page.request.post(u, headers=h, json={}, timeout=20000)", True, "PW001"),
    ("BAD", "resp = page.request.put(u, json={})", True, "PW001"),
    ("BAD", "resp = page.request.post(u, files={'f': b'x'})", True, "PW002"),
    ("BAD", "resp = page.request.get(u, verify=False)", True, "PW003"),
    ("BAD", "resp = page.request.get(u, allow_redirects=True)", True, "PW004"),
    ("GOOD", "resp = page.request.post(u, headers=h, data=json.dumps(p), timeout=20000)", False, None),
    ("GOOD", "resp = page.request.get(u, params={'a': '1'})", False, None),
]


def _selftest() -> int:
    print("自检规则有效性（BAD 必须命中 / GOOD 必须不命中）")
    print("-" * 72)
    failed = 0
    for label, code, should_hit, expect_rule in SELFTEST_CASES:
        hits = _scan(ast.parse(code), "selftest.py")
        got = bool(hits)
        ok = (got == should_hit)
        if ok and should_hit and expect_rule:
            ok = any(h["rule"] == expect_rule for h in hits)
        print("%s %-5s %-62s -> hits=%d%s"
              % ("ok  " if ok else "FAIL", label, code[:62], len(hits),
                 " (%s)" % hits[0]["rule"] if hits else ""))
        if not ok:
            failed += 1
    print("-" * 72)
    if failed:
        print("❌ 自检失败 %d/%d 条：规则未生效，门禁不可信" % (failed, len(SELFTEST_CASES)))
        return 1
    print("✅ 自检通过 %d/%d 条，规则有效" % (len(SELFTEST_CASES), len(SELFTEST_CASES)))
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    # requests 若真被引入，说明项目里混用两种 HTTP 客户端，本规则的假设不成立 → 不误报
    uses_requests = False
    for path in _iter_py_files():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "import requests" in src or "from requests" in src:
            uses_requests = True
            break

    if uses_requests:
        print("检测到项目使用了 requests，PW00x 规则（requests 参数误用到 Playwright）自动停用，避免误报")
        return 0

    all_hits = []
    for path in _iter_py_files():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        all_hits.extend(_scan(tree, path))

    print("扫描 src/*.py 的 Playwright API 调用，规则: %s"
          % ", ".join(r["id"] for r in RULES))
    print("-" * 72)
    if not all_hits:
        print("✅ 未发现 requests 参数误用到 Playwright 的情况")
        return 0

    for h in all_hits:
        print("FAIL [%s] %s:%d  .%s(..., %s=...)"
              % (h["rule"], h["file"], h["line"], h["method"], h["kw"]))
        print("       %s" % h["desc"])
        print("       → %s" % h["hint"])
    print("-" * 72)
    print("❌ 共 %d 处 Playwright API 误用" % len(all_hits))
    return 1


if __name__ == "__main__":
    sys.exit(main())
