"""本地无浏览器验证：覆盖本次改动的关键决策逻辑（无需 Chromium / 无需联网）。

运行：
    python tests/local_verify.py
要求：仅依赖 Python 标准库（unittest.mock）。会通过 sys.path + stub 的方式
导入 src/account_runner.py 与 src/report.py，而不会加载 playwright / 真实浏览器。

覆盖点：
  1. 每日一炼 histscore 映射 = 「AI运动会」(+2)
  2. _verify_task_via_histscore：命中 / 未命中 / 旧日期 / 名字列兜底
  3. _run_quiz 跳过分支：skip_if_missing=True -> skipped（不再误报 failed）
  4. report 分母恢复为 5 项 Web 任务、跳过计数、页面变动告警、save() 生成 md
"""
import sys, os, types, datetime, tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)


def _stub(name, attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


_stub("browser", {"Browser": object})
_stub("utils", {"aes_encrypt_for_frontend": lambda *a, **k: "",
                "solve_numeric_captcha_from_cookies": lambda *a, **k: ""})
_stub("walk", {"run_walk": lambda *a, **k: {}})

import account_runner as ar
import report as rep

failures = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# 1. histscore 映射
check("histscore daily_refine -> ('AI运动会', None)",
      ar._histscore_match("daily_refine") == ("AI运动会", None))
check("histscore daily_answer -> ('限时活动I','每日一答')",
      ar._histscore_match("daily_answer") == ("限时活动I", "每日一答"))
check("histscore daily_learn -> ('限时活动III', None)",
      ar._histscore_match("daily_learn") == ("限时活动III", None))
check("histscore 未知任务 -> (None, None)",
      ar._histscore_match("nope") == (None, None))

# 2. _verify_task_via_histscore（纯函数）
today = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%m-%d")
rows_hit  = [{"cat": "AI运动会", "name": "活动1", "score": "+2", "time": today + " 09:00"}]
rows_miss = [{"cat": "其他",     "name": "x",    "score": "+2", "time": today + " 09:00"}]
rows_old  = [{"cat": "AI运动会", "name": "活动1", "score": "+2", "time": "01-01 09:00"}]
rows_name = [{"cat": "其它类别", "name": "AI运动会-热身", "score": "+2", "time": today + " 10:00"}]
rows_ans  = [{"cat": "限时活动I", "name": "每日一答", "score": "+10", "time": today + " 08:00"}]

r1 = ar._verify_task_via_histscore(rows_hit, "daily_refine", today)
check("verify daily_refine 命中 -> done", r1.get("done") is True)
check("verify daily_refine 得分解析=2", ar._parse_score(r1.get("score")) == 2)
check("verify daily_refine 未命中 -> 未完成",
      ar._verify_task_via_histscore(rows_miss, "daily_refine", today).get("done") is False)
check("verify daily_refine 旧日期 -> 未完成",
      ar._verify_task_via_histscore(rows_old, "daily_refine", today).get("done") is False)
check("verify daily_refine 名字列命中",
      ar._verify_task_via_histscore(rows_name, "daily_refine", today).get("done") is True)
check("verify daily_answer 命中",
      ar._verify_task_via_histscore(rows_ans, "daily_answer", today).get("done") is True)
check("parse +10 -> 10", ar._parse_score("+10") == 10)
check("parse 10分 -> 10", ar._parse_score("10分") == 10)
check("parse None -> None", ar._parse_score(None) is None)

# 3. _run_quiz 跳过分支（mock 页面）
with mock.patch.object(ar, "_goto", lambda *a, **k: None), \
     mock.patch.object(ar, "_click_text", lambda *a, **k: False):
    d_skip = ar._run_quiz(None, "每日一看", 5, "http://x", skip_if_missing=True)
    d_fail = ar._run_quiz(None, "每日一看", 5, "http://x", skip_if_missing=False)
check("run_quiz skip_if_missing=True -> status=skipped", d_skip["status"] == "skipped")
check("run_quiz 跳过原因含「跳过」", "跳过" in (d_skip.get("reason") or ""))
check("run_quiz skip_if_missing=False -> status=failed", d_fail["status"] == "failed")

# 4. report 分母 / 跳过计数 / 页面变动告警 / save()
WEB = rep.WEB_TASK_ORDER
check("WEB_TASK_ORDER 含 5 项", len(WEB) == 5)
check("WEB_TASK_ORDER 含 daily_view 与 daily_practice",
      ("daily_view" in WEB) and ("daily_practice" in WEB))

def mkacc(statuses):
    tasks = {k: {"status": statuses.get(k, "done")} for k in WEB}
    return {"user": "U1", "tasks": tasks, "initial_points": 100,
            "final_points": 110, "status": "success"}

done, skip, fail, fi = rep._account_verdict(mkacc({})["tasks"])
check("verdict 全完成 -> done=5", done == 5 and skip == 0 and fail == 0)
done, skip, fail, fi = rep._account_verdict(mkacc({"daily_view": "skipped"})["tasks"])
check("verdict 一个跳过 -> skip=1, done=4", skip == 1 and done == 4 and fail == 0)
overall, overview, quick = rep._build_summary([mkacc({})])
check("summary含「答题 5/5」", any("答题 5/5" in q for q in quick))
overall, overview, quick = rep._build_summary([mkacc({"daily_view": "skipped"})])
check("summary含「跳过 1」", any("跳过 1" in q for q in quick))
alert_acc = mkacc({}); alert_acc["page_change_alert"] = {
    "changed": True, "added": [{"container": ".humanSociety-link", "item": "每日一看"}],
    "removed": [], "container_changed": True}
overall, overview, quick = rep._build_summary([alert_acc])
check("summary含「页面监控区发生变动」告警", any("页面监控区发生变动" in o for o in overview))

tmp = tempfile.mkdtemp(prefix="local_verify_")
old = os.getcwd()
try:
    os.chdir(tmp)
    rep.save([mkacc({})])
finally:
    os.chdir(old)
mdp = os.path.join(tmp, "reports", datetime.date.today().strftime("%Y-%m-%d") + ".md")
check("report md 已生成", os.path.exists(mdp))
with open(mdp, encoding="utf8") as f:
    check("md 含「答题 5/5」", "答题 5/5" in f.read())

print("\n=== RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
