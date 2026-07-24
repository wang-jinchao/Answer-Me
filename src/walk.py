#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upfitapp 每日一走 —— liteapp 步数写入（集成版，供 account_runner 调用）

============================================================
⚠️ 凭证安全（最高优先级，与答题任务同一规则）
------------------------------------------------------------
1. 本模块不内置任何真实 <openId> / <unionid> / 姓名。
2. openid/unionid 来自 ACCOUNTS_JSON（GitHub Secret，运行时注入），绝不落盘到仓库。
3. 执行结束调用方应立即清除本次传入的临时值（Secret 本身不在仓库，安全）。

机制说明（来自 upfit-daily-walk 技能实测，2026-07-24 修正）：
- Web 端 RS 账号无法写入步数；步数落库走微信小程序 liteapp 接口（openId 体系）。
- **写入闸门 = usrreg 建立的同一 PHPSESSID 会话**；usrreg→index(判绑定人)→uploadstep(写今天) 同 CookieJar 即落库。
- 假成功（result:true 但不落库）的真正根因是 cookie 不共享 / 漏 usrreg，而非 decrypt 失败。
- decrypt 仅用于回读 30 天历史；站点维护中只阻断历史回读、**不阻断写入**。本集成版只传今天一条，无需 decrypt，也不依赖 enc/iv/key。
- openId 当前绑在谁，uploadstep 就写给谁；写前必校验 index.userName，非目标立即停，防误写。
- 步数约束：每天只能 ≥ 当前服务端存储值（递增/相等），递减或暴涨被拒；只传今天最安全。
"""

import json
import logging
import random
import time
import urllib.parse
import urllib.request
import http.cookiejar

logger = logging.getLogger(__name__)

BASE_URL = "https://www.upfitapp.com"
LITEAPP = f"{BASE_URL}/liteapp"
APPID = "wx973376eb86ed6649"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75")
REFERER = f"https://servicewechat.com/{APPID}/8/page-frame.html"
VERSION = 5

# 每日步数同步区间（用户要求：区间 [10000, 12000] 随机，只同步当天）
WALK_STEP_MIN = 10000
WALK_STEP_MAX = 12000


def _build_opener():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(),
    )
    return opener, cj


def _get_json(opener, url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    with opener.open(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 站点维护/异常时可能返回非 JSON（如「站点维护中」HTML），透传给调用方判断
        return {"_raw": raw}


def _usrreg(opener, openid, unionid):
    return _get_json(opener, f"{LITEAPP}/usrreg?openid={openid}&unionid={unionid}&version={VERSION}")


def _index(opener, openid, unionid):
    return _get_json(opener, f"{LITEAPP}/index?openId={openid}&unionid={unionid}&version={VERSION}")


def _uploadstep(opener, openid, step_info_list):
    si = urllib.parse.quote(json.dumps(step_info_list, separators=(",", ":")))
    url = f"{LITEAPP}/uploadstep?stepInfo={si}&openId={openid}&version={VERSION}"
    return _get_json(opener, url)


def _today_zero_ts_east8():
    import datetime
    beijing = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(beijing)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _resolve_target_step(walk_step):
    """解析今日目标步数：优先用配置的 walk_step，但必须落在 [WALK_STEP_MIN, WALK_STEP_MAX] 内；
    否则（未配置 / 越界 / 非数字）用区间内随机整数。
    """
    if isinstance(walk_step, int) and WALK_STEP_MIN <= walk_step <= WALK_STEP_MAX:
        return walk_step
    return random.randint(WALK_STEP_MIN, WALK_STEP_MAX)


def run_walk(account: dict) -> dict:
    """为单个账号执行每日一走（仅当配置了 openid 时由调用方决定调用）。

    写入闸门 = usrreg 建立的同一 PHPSESSID 会话；本集成版只传「今天一条」记录，
    **不需要 decrypt、不依赖 enc/iv/key**（decrypt 仅回读 30 天历史，维护态不阻断写入）。

    返回结构化结果 dict：
        {status, bound_name, before_step, target_step, after_step, reason,
         motion_score, reach_score, today_walk_score}
    status ∈ {done, failed, skipped}

    绑定校验：仅用 account['walk_name'] 比对 index.userName（防误写），与账号显示名 name 无关。
    写入说明：uploadstep 调用后**不验证是否写入成功**，直接标记 done。
    得分汇总：调用方在 run_walk 之后读 histscore 计算「每日运动+步数达标」求和注入本结果。
    """
    openid = account.get("openid")
    unionid = account.get("unionid")
    if not openid or not unionid:
        return {"status": "skipped", "reason": "账号未配置 openid/unionid，跳过每日一走"}

    # 期望绑定人：只用 walk_name（防误写校验，启用 walk 时必填）；绝不退回 name。
    target_name = account.get("walk_name") or ""
    walk_step = account.get("walk_step")  # 可选：今日目标步数；[10000,12000] 内生效，否则随机

    result = {
        "status": "failed",
        "bound_name": None,
        "before_step": None,
        "target_step": None,
        "after_step": None,
        "reason": "",
        "motion_score": None,
        "reach_score": None,
        "today_walk_score": None,
    }

    opener, _ = _build_opener()

    # ① usrreg 建立会话
    try:
        _usrreg(opener, openid, unionid)
    except Exception as e:
        result["reason"] = f"usrreg 失败：{e}"
        return result

    # 判定当前绑定人（防误写）
    try:
        idx = _index(opener, openid, unionid).get("rtn", {})
    except Exception as e:
        result["reason"] = f"index 判定绑定人失败：{e}"
        return result
    bound = idx.get("userName", "?")
    result["bound_name"] = bound
    result["before_step"] = idx.get("userStep")
    logger.info("每日一走 账号 %s 当前绑定人=%s userStep=%s", target_name or openid, bound, idx.get("userStep"))

    if target_name and target_name not in str(bound):
        result["status"] = "failed"
        result["reason"] = (f"绑定人不是目标【{target_name}】（当前绑定={bound}），"
                            f"已停止，未做任何写入；请先在微信小程序登录目标账号切回绑定")
        logger.warning("每日一走 绑定漂移：期望 %s 实际 %s，中止防误写", target_name, bound)
        return result

    # 注意：本集成版不调用 decrypt。写入闸门是 usrreg 同 PHPSESSID 会话，
    # decrypt 仅回读 30 天历史，维护态不阻断写入；只传今天一条无需历史。

    # 决定今日目标步数：优先用配置的 walk_step，但须落在 [WALK_STEP_MIN, WALK_STEP_MAX]；否则区间随机。
    # 用户要求：每天只执行一次、只同步当天、步数落在 [10000,12000] 随机。
    current_step = idx.get("userStep") or 0
    target = _resolve_target_step(walk_step)
    result["target_step"] = target

    # 只传今天这一条：每次运行仅提交「当天 = 目标步数」的单条记录。
    # 用户已确认：不在意后端是合并还是覆盖历史，只要今天这条能传上去即可（已清理 30 天历史合并逻辑）。
    tz = _today_zero_ts_east8()
    step_info = [{"timestamp": tz, "step": target}]

    # ③ uploadstep（用户要求：不验证是否写入成功，调后即标记 done）
    try:
        up = _uploadstep(opener, openid, step_info)
    except Exception as e:
        result["reason"] = f"uploadstep 失败：{e}"
        return result
    up_ok = up.get("result", {}).get("result") if isinstance(up.get("result"), dict) else up.get("result")
    logger.info("每日一走 uploadstep result=%s（不验证写入结果）", up_ok)

    result["after_step"] = target
    result["status"] = "done"
    result["reason"] = f"已提交今日步数 {target}（写入前 {current_step}，区间随机 [10000,12000]）"
    logger.info("每日一走 已提交：%s -> %s", current_step, target)

    return result
