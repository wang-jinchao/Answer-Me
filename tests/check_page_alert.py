"""校验页面变动告警口径（纯逻辑，本地可跑，不需要浏览器/网络）。

背景：2026-09-20 用户最终指定监控范围 —— **只看 .humanSociety-link 与
.dailyActivityWarp 两个容器**的内容或元素是否变化。轮播图、整页主区哈希等
其余区域一律不采集也不告警（对运营换图/倒计时/随机推荐过于敏感，实测每次
运行必变，纯噪音）。

本脚本对 `_diff_signature`（已抽成不依赖浏览器的纯函数）做场景验证，
确保既不漏报该报的、也不再被噪音淹没。

import account_runner 需要 playwright 等重依赖，这里用 sys.modules 打桩
（只桩掉 browser / utils / walk 三个本项目模块，被测函数本身是真实代码）。
用法：python tests/check_page_alert.py     退出码 0 = 全部通过
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# 打桩被测模块的重依赖（account_runner 顶层 from ... import ...）
_stub("browser", Browser=object)
_stub("utils", aes_encrypt_for_frontend=lambda *a, **k: "",
      solve_numeric_captcha_from_cookies=lambda *a, **k: None)
_stub("walk", run_walk=lambda *a, **k: None)

import account_runner as ar  # noqa: E402

SEL_A = '.humanSociety-link'
SEL_B = '.dailyActivityWarp'


def item(tag, cls, text, href="", src=""):
    return {"tag": tag, "cls": cls, "text": text, "href": href, "src": src}


def cont(items, found=True, hash_="h0"):
    return {"found": found, "count": len(items), "items": items, "hash": hash_}


def sig(a_items=None, b_items=None, a_found=True, b_found=True,
        a_hash="h0", b_hash="h0"):
    a_items = [item("a", "act", "每日一学", "/a?daily=1"),
               item("a", "act", "每日一看", "/b?daily=1")] if a_items is None else a_items
    b_items = [item("a", "act", "限时活动", "/c")] if b_items is None else b_items
    return {"containers": {SEL_A: cont(a_items, a_found, a_hash),
                           SEL_B: cont(b_items, b_found, b_hash)}}


def diff(prev, cur):
    return ar._diff_signature(prev, cur)


def main() -> int:
    cases = []

    # 1) 两容器完全不变 → 不告警
    cases.append(("两容器完全无变化 → 不告警", diff(sig(), sig())["changed"] is False))

    # 2) humanSociety-link 新增一个元素 → 告警
    prev = sig()
    cur = sig(a_items=[item("a", "act", "每日一学", "/a?daily=1"),
                       item("a", "act", "每日一看", "/b?daily=1"),
                       item("a", "act", "每日一炼", "/c?daily=1")])
    a = diff(prev, cur)
    cases.append(("%s 新增元素 → 告警" % SEL_A,
                  a["changed"] is True and len(a["added"]) == 1 and a["container_changed"]))

    # 3) dailyActivityWarp 消失一个元素 → 告警
    prev = sig(b_items=[item("a", "act", "限时活动", "/c"), item("a", "act", "另一个", "/d")])
    cur = sig(b_items=[item("a", "act", "限时活动", "/c")])
    a = diff(prev, cur)
    cases.append(("%s 消失元素 → 告警" % SEL_B,
                  a["changed"] is True and len(a["removed"]) == 1))

    # 4) 元素没增删、但容器内容(哈希)变了 → 告警（用户要求"内容变化"也要报）
    a = diff(sig(a_hash="h0"), sig(a_hash="h9"))
    cases.append(("元素未变但容器内容变化 → 告警",
                  a["changed"] is True and a["container_changed"] is True))

    # 5) 容器从"找不到"变成"找到" → 告警（可能是站点改版补回来了）
    a = diff(sig(a_found=False, a_hash=None), sig(a_found=True))
    cases.append(("容器由未找到变为找到 → 告警", a["changed"] is True))

    # 6) 容器从"找到"变成"找不到" → 告警（页面结构被改掉，必须知道）
    a = diff(sig(b_found=True), sig(b_found=False, b_hash=None))
    cases.append(("容器消失 → 告警", a["changed"] is True and SEL_B in a["container_missing"]))

    # 7) 旧基线没有 containers 结构 → 不得误报（改版后首次只记录新基线）
    old = {"links": [{"t": "每日一学", "h": "/a"}], "main_hash": "h0"}
    a = diff(old, sig())
    cases.append(("旧基线无 containers → 不误报", a["changed"] is False))

    print("校验页面变动告警口径（只看 humanSociety-link / dailyActivityWarp）")
    print("-" * 72)
    failed = 0
    for name, ok in cases:
        print("%s %s" % ("ok  " if ok else "FAIL", name))
        if not ok:
            failed += 1
    print("-" * 72)
    if failed:
        print("❌ %d/%d 条不通过" % (failed, len(cases)))
        return 1
    print("✅ 全部通过 %d/%d" % (len(cases), len(cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
