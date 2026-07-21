
import datetime
import json
import os


TASK_ORDER = ("login", "daily_learn", "daily_view", "daily_practice", "daily_answer")
_TASK_LABELS = {
    "login": "登录",
    "daily_learn": "每日一学",
    "daily_view": "每日一看",
    "daily_practice": "每日一练",
    "daily_answer": "每日一答",
}


def _task_line(task_result):
    if not isinstance(task_result, dict):
        return "- 状态：未知"
    status = task_result.get("status")
    if status == "done":
        extra = ""
        if "answered" in task_result:
            extra = f"（答对记录 {task_result['answered']} 题）"
        return f"- 状态：**完成** ✅ {extra}"
    if status == "skipped":
        return "- 状态：跳过 ⚪"
    if status == "no_attempts":
        return "- 状态：今日已无次数（跳过）⚪"
    reason = task_result.get("reason") or task_result.get("error") or ""
    return f"- 状态：**失败** ❌ {reason}"


def _account_verdict(tasks):
    done = skip = fail = 0
    failed_items = []
    for key in TASK_ORDER:
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
                quick_lines.append(f"- {user}：❌ 有失败（完成 {done}/5）— {failed_str}{gain_txt}")
        else:
            quick_lines.append(f"- {user}：✅ 完整（完成 {done}/5，跳过 {skip}）{gain_txt}")

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
