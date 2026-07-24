
import datetime
import json
import os


TASK_ORDER = ("login", "daily_learn", "daily_view", "daily_practice", "daily_answer", "daily_walk")
_TASK_LABELS = {
    "login": "登录",
    "daily_learn": "每日一学",
    "daily_view": "每日一看",
    "daily_practice": "每日一练",
    "daily_answer": "每日一答",
    "daily_walk": "每日一走",
}

# 每日一走走独立 liteapp 接口，并非所有账号都配置；统计"答题完成数"分母时只算 Web 四项。
WEB_TASK_ORDER = ("daily_learn", "daily_view", "daily_practice", "daily_answer")


def _task_line(task_result):
    if not isinstance(task_result, dict):
        return "- 状态：未知"
    status = task_result.get("status")
    if status == "done":
        extra = ""
        if "answered" in task_result:
            extra = f"（答对记录 {task_result['answered']} 题）"
        if "before_step" in task_result or "today_walk_score" in task_result:
            b, a = task_result.get("before_step"), task_result.get("after_step")
            if b is not None or a is not None:
                extra = f"（步数 {b} → {a}）"
            ws = task_result.get("today_walk_score")
            if ws is not None:
                extra += f"，今日得分 +{ws}（每日运动+步数达标）"
        return f"- 状态：**完成** ✅ {extra}"
    if status == "skipped":
        return "- 状态：跳过 ⚪"
    if status == "no_attempts":
        return "- 状态：今日已无次数（跳过）⚪"
    reason = task_result.get("reason") or task_result.get("error") or ""
    return f"- 状态：**失败** ❌ {reason}"


def _account_verdict(tasks):
    """按 Web 答题四项统计完成度；每日一走作为独立可选任务单独计入 fail（若失败）。

    返回 (done, skip, fail, failed_items)，其中 done/skip/fail 仅覆盖 WEB_TASK_ORDER，
    便于速览显示「完成 X/4」；walk 失败会在 failed_items 中体现但不影响分母。
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

    for r in results:
        user = r.get("user") or "未知用户"
        tasks = r.get("tasks", {}) or {}
        if not isinstance(tasks, dict):
            tasks = {}
        done, skip, fail, failed_items = _account_verdict(tasks)
        init = r.get("initial_points")
        final = r.get("final_points")
        gain_txt = ""
        if isinstance(init, int) and isinstance(final, int):
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
                quick_lines.append(f"- {user}：❌ 有失败（答题 {done}/4）— {failed_str}{gain_txt}")
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
            quick_lines.append(f"- {user}：✅ 完整（答题 {done}/4，跳过 {skip}）{walk_txt}{gain_txt}")

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

    return overall, overview, quick_lines


def save(results):
    os.makedirs("reports", exist_ok=True)
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

    for r in results:
        user = r.get("user") or "未知用户"
        tasks = r.get("tasks", {}) or {}
        if not isinstance(tasks, dict):
            tasks = {}
        init = r.get("initial_points")
        final = r.get("final_points")
        gained = ""
        if isinstance(init, int) and isinstance(final, int):
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
                    lines.append(f"  - 绑定人：{wk.get('bound_name')}")
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
