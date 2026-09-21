
import datetime
import json
import os
import sys


TASK_ORDER = ("login", "daily_learn", "daily_view", "daily_refine", "daily_practice", "daily_answer", "daily_walk")
_TASK_LABELS = {
    "login": "登录",
    "daily_learn": "每日一学",
    "daily_view": "每日一看",
    "daily_practice": "每日一练",
    "daily_refine": "每日一炼",
    "daily_answer": "每日一答",
    "daily_walk": "每日一走",
}

# 每日一走是独立 liteapp 接口，并非所有账号都配置；分母只算 Web 活跃任务（见 WEB_TASK_ORDER）。
# 2026-09-19 恢复：每日一看/每日一练 重新纳入每日执行（入口找不到则跳过），故 WEB_TASK_ORDER 恢复为五项。
WEB_TASK_ORDER = ("daily_learn", "daily_view", "daily_refine", "daily_practice", "daily_answer")


def _mask_name(name):
    """对绑定人真实姓名脱敏，避免报告明文暴露敏感身份关联（openid↔姓名）。"""
    s = (name or "").strip()
    if not s:
        return "?"
    if len(s) == 1:
        return s + "***"
    return s[0] + "***" + s[-1]


def _task_line(task_result):
    if not isinstance(task_result, dict):
        return "- 状态：未知"
    status = task_result.get("status")
    if status in ("done", "done_with_skips"):
        extra = ""
        if "answered" in task_result:
            extra = f"（答对记录 {task_result['answered']} 题"
            if task_result.get("skipped"):
                extra += f"，跳过 {task_result['skipped']} 题"
            extra += "）"
        if "before_step" in task_result or "today_walk_score" in task_result:
            b, a = task_result.get("before_step"), task_result.get("after_step")
            if b is not None or a is not None:
                extra = f"（步数 {b} → {a}）"
            ws = task_result.get("today_walk_score")
            if ws is not None:
                extra += f"，今日得分 +{ws}（每日运动+步数达标）"
        if status == "done_with_skips":
            return f"- 状态：**完成（有跳过）** ⚠️ {extra}"
        # 区分「本页真跑完」与「靠积分明细反查判成完成」：
        # 后者本次并未实际执行成功，若混在一起显示会造成「报告全绿、实际没做」的假象。
        upgraded_from = task_result.get("upgraded_from")
        if upgraded_from and upgraded_from not in ("done", "done_with_skips"):
            return (f"- 状态：**完成（按积分明细判定）** 🟡 {extra}"
                    f"｜本次实际执行：{upgraded_from}")
        return f"- 状态：**完成** ✅ {extra}"
    if status == "skipped":
        return "- 状态：跳过 ⚪"
    if status == "no_attempts":
        return "- 状态：今日已无次数（跳过）⚪"
    reason = task_result.get("reason") or task_result.get("error") or ""
    return f"- 状态：**失败** ❌ {reason}"


def _account_verdict(tasks):
    """按 Web 活跃任务统计完成度（当前五项：每日一学/每日一看/每日一炼/每日一练/每日一答）；
    每日一走作为独立可选任务单独计入 fail（若失败）。

    返回 (done, skip, fail, failed_items)，其中 done/skip/fail 仅覆盖 WEB_TASK_ORDER，
    便于速览显示「完成 X/N」（N=活跃 Web 任务数）；walk 失败会在 failed_items 中体现但不影响分母。
    """
    done = skip = fail = 0
    failed_items = []
    for key in WEB_TASK_ORDER:
        t = tasks.get(key)
        label = _TASK_LABELS.get(key, key)
        if not isinstance(t, dict):
            fail += 1
            failed_items.append((label, "未执行（结果缺失）"))
            continue
        st = t.get("status")
        if st == "done":
            done += 1
        elif st == "done_with_skips":
            answered = t.get("answered")
            expected = t.get("expected")
            if answered is not None and expected is not None and answered < expected:
                # ★ 关键修正：P1 产生的少答是 skipped=0、answered<expected（末题点不中），
                # 头条必须如实计为未完成，否则仍会显示"完成 X/N"掩盖真实少答。
                fail += 1
                failed_items.append((label, f"实际答对 {answered}/{expected} 题（少 {expected - answered} 题）"))
            else:
                done += 1
                if t.get("skipped"):
                    failed_items.append((label, f"有 {t['skipped']} 题解析失败被跳过"))
        elif st in ("skipped", "no_attempts"):
            skip += 1
        else:
            fail += 1
            reason = t.get("reason") or t.get("error") or "失败"
            failed_items.append((label, reason))

    # 每日一走（可选）：失败计入总 fail 与 failed_items，但不改变答题分母
    walk = tasks.get("daily_walk")
    if isinstance(walk, dict) and walk.get("status") not in ("done", "skipped"):
        fail += 1
        reason = walk.get("reason") or walk.get("error") or "失败"
        failed_items.append((_TASK_LABELS.get("daily_walk", "每日一走"), reason))

    return done, skip, fail, failed_items


def _build_summary(results):
    total_accounts = len(results)
    has_problem = False
    quick_lines = []
    total_gain = 0
    gain_accounts = 0

    # 根治：每账号用 histscore 账本核算的今日净增作为权威总分（覆盖多网址漏算）。
    # 仅最后一个网址的结果里带 histscore_today_gain，按账号去重取一次。
    acct_hs_gain = {}
    for r in results:
        hg = r.get("histscore_today_gain")
        if isinstance(hg, int):
            acct_hs_gain[r.get("user")] = hg

    counted = set()
    for r in results:
        user = r.get("user") or "未知用户"
        tasks = r.get("tasks", {}) or {}
        if not isinstance(tasks, dict):
            tasks = {}
        done, skip, fail, failed_items = _account_verdict(tasks)
        init = r.get("initial_points")
        final = r.get("final_points")
        gain_txt = ""
        if user in acct_hs_gain and user not in counted:
            # 积分明细账本核算的今日净增（跨两网址），优先于逐网址首页读数（后者在第二网址超时=未知）。
            g = acct_hs_gain[user]
            gain_txt = f"，{g:+} 分（积分明细核算）"
            total_gain += g
            gain_accounts += 1
            counted.add(user)
        elif isinstance(init, int) and isinstance(final, int):
            g = final - init
            gain_txt = f"，{g:+} 分"
            total_gain += g
            gain_accounts += 1

        if fail > 0 or r.get("status") != "success":
            has_problem = True
            if not tasks:
                err = r.get("error") or "未知错误"
                quick_lines.append(f"- {user}：❌ 登录/执行失败 — {err}{gain_txt}")
            else:
                failed_str = "；".join(f"{lbl}：{rsn}" for lbl, rsn in failed_items) or "未知错误"
                quick_lines.append(f"- {user}：❌ 有失败（答题 {done}/{len(WEB_TASK_ORDER)}）— {failed_str}{gain_txt}")
        else:
            walk = tasks.get("daily_walk")
            walk_txt = ""
            if isinstance(walk, dict):
                ws = walk.get("status")
                if ws == "done":
                    walk_txt = "，每日一走✅"
                elif ws == "skipped":
                    walk_txt = "，每日一走⚪(跳过)"
                else:
                    walk_txt = "，每日一走❌"
            quick_lines.append(f"- {user}：✅ 完整（答题 {done}/{len(WEB_TASK_ORDER)}，跳过 {skip}）{walk_txt}{gain_txt}")

    if total_accounts == 0:
        overall = "⚠️ 无账号数据"
    elif has_problem:
        overall = "❌ 有任务未完成"
    else:
        overall = "✅ 全部完成"

    overview = [
        f"- 判定：**{overall}**",
        f"- 账号数：{total_accounts}",
    ]
    if gain_accounts:
        overview.append(f"- 累计获得积分：{total_gain:+} 分（{gain_accounts} 个账号有记录）")

    # 页面较上一日变动告警（口径 2026-09-20 最终：只监控 .humanSociety-link 与
    # .dailyActivityWarp 两个容器的内容/元素变化；其余区域一律不看）
    page_changes = [r for r in results
                    if isinstance(r.get("page_change_alert"), dict) and r["page_change_alert"].get("changed")]
    if page_changes:
        n_elem = sum(len(r["page_change_alert"].get("added") or [])
                     + len(r["page_change_alert"].get("removed") or [])
                     for r in page_changes)
        n_cont = sum(1 for r in page_changes
                     if r["page_change_alert"].get("container_changed")
                     or r["page_change_alert"].get("container_missing")
                     or r["page_change_alert"].get("container_appeared"))
        overview.append(f"- ⚠️ 页面监控区发生变动（{len(page_changes)} 个账号命中）："
                        f"内容变化 {n_cont} 处、元素增删 {n_elem} 个"
                        f"（监控范围：humanSociety-link / dailyActivityWarp）")

    return overall, overview, quick_lines


def save(results):
    os.makedirs("reports", exist_ok=True)
    # 本地 GBK 控制台下含 emoji 的 print 会抛 UnicodeEncodeError；reconfigure 到 utf-8 避免崩溃（GA/Linux 本就是 utf-8，无副作用）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    today = datetime.date.today()
    json_path = os.path.join("reports", f"{today}.json")
    md_path = os.path.join("reports", f"{today}.md")

    with open(json_path, "w", encoding="utf8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    overall, overview, quick_lines = _build_summary(results)

    lines = [f"# 每日答题汇总 {today}", ""]
    lines.append("## 总览")
    lines.extend(overview)
    lines.append("")
    lines.append("### 账号速览")
    lines.extend(quick_lines)
    lines.append("")
    lines.append("---")
    lines.append("")

    # 每账号用 histscore 账本核算的今日净增作为权威总分；按账号去重仅展示一次。
    accounts_with_hs = {r.get("user") for r in results
                        if isinstance(r.get("histscore_today_gain"), int)}
    hs_shown = set()

    for r in results:
        user = r.get("user") or "未知用户"
        tasks = r.get("tasks", {}) or {}
        if not isinstance(tasks, dict):
            tasks = {}
        init = r.get("initial_points")
        final = r.get("final_points")
        hg = r.get("histscore_today_gain")
        gained = ""
        if isinstance(hg, int) and user not in hs_shown:
            gained = f"，本次获得 **{hg}** 分（积分明细核算）"
            hs_shown.add(user)
        elif user in accounts_with_hs:
            gained = "（今日合计见积分明细核算）"
        elif isinstance(init, int) and isinstance(final, int):
            gained = f"，本次获得 **{final - init}** 分"
        lines.append(f"## 账号：{user}")
        lines.append(f"- 初始积分：{init if init is not None else '未知'}")
        lines.append(f"- 最终积分：{final if final is not None else '未知'}{gained}")
        for key in TASK_ORDER:
            if key not in tasks:
                continue
            lines.append(f"### {_TASK_LABELS.get(key, key)}")
            lines.append(_task_line(tasks[key]))

            if key == "daily_learn" and isinstance(tasks[key], dict) and "api_result" in tasks[key]:
                lines.append(f"  - API 返回：{tasks[key]['api_result']}")
            if key == "daily_walk" and isinstance(tasks[key], dict):
                wk = tasks[key]
                if wk.get("bound_name") is not None:
                    lines.append(f"  - 绑定人：{_mask_name(wk.get('bound_name'))}")
                if wk.get("before_step") is not None or wk.get("after_step") is not None:
                    lines.append(f"  - 步数：{wk.get('before_step')} → {wk.get('after_step')}")
                motion, reach, total = wk.get("motion_score"), wk.get("reach_score"), wk.get("today_walk_score")
                if total is not None:
                    lines.append(f"  - 今日得分汇总：每日运动 {motion} + 步数达标 {reach} = **+{total}**（正常应为 +10）")
        if r.get("error"):
            lines.append(f"- 错误：{r['error']}")
        lines.append("")

    content = "\n".join(lines)
    with open(md_path, "w", encoding="utf8") as f:
        f.write(content)


    print("=" * 40)
    print(f"每日答题汇总 {today} —— {overall}")
    for q in quick_lines:
        print(q)
    print("=" * 40)
    print(f"详细报告：{json_path} / {md_path}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf8") as f:
                f.write(f"# 每日答题汇总 {today}\n\n")
                f.write("## 总览\n")
                f.write("\n".join(overview) + "\n\n")
                f.write("### 账号速览\n")
                f.write("\n".join(quick_lines) + "\n")
        except Exception:
            pass

    return overall
