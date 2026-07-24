
import json
import os


_USER_KEYS = ("username", "user", "account", "acct", "name_id", "loginname")
_PASS_KEYS = ("password", "pass", "pwd", "passwd")
_NAME_KEYS = ("name", "nickname", "alias", "label")


def _pick(d, keys):
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _normalize_account(raw, idx):
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"ACCOUNTS_JSON 第 {idx} 个元素必须是对象 {{...}}，实际是 {type(raw).__name__}"
        )
    username = _pick(raw, _USER_KEYS)
    password = _pick(raw, _PASS_KEYS)
    name = _pick(raw, _NAME_KEYS) or username
    problems = []
    if not username:
        problems.append("缺少 username（也可用 user/account）")
    if not password:
        problems.append("缺少 password（也可用 pass/pwd）")
    if problems:
        raise RuntimeError(
            f"ACCOUNTS_JSON 第 {idx} 个账号(name={name!r}) 配置无效：" + "；".join(problems)
            + f"。原始字段：{list(raw.keys())}"
        )
    # 每日一走（可选）：openid/unionid 成对出现，有 openid 即启用；walk_name 为目标绑定人
    # （防误写校验，启用 walk 时事实必填）；walk_step 为今日目标步数（可选，[10000,12000] 内
    # 生效否则随机）。注意：微信运动加密包 walk_enc/walk_iv/walk_key 属「会话级动态运行时输入」，
    # 每次从微信小程序实时请求抓取，**不放在静态 ACCOUNTS_JSON**——由运行时环境变量
    # WALK_ENC/WALK_IV/WALK_KEY 注入（见 account_runner / walk.run_walk 参数），避免写死失效。
    walk_fields = ("openid", "unionid", "walk_name", "walk_step")
    acc = {"name": name, "username": username, "password": password}
    for wf in walk_fields:
        if wf in raw and str(raw[wf]).strip() != "":
            val = raw[wf]
            # walk_step 容错：JSON 里写成字符串 "11000" 也能被 _resolve_target_step 识别
            if wf == "walk_step":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue  # 非法整数则忽略，run_walk 会改用区间随机
            acc[wf] = val
    return acc


def load_accounts():
    s = os.environ.get("ACCOUNTS_JSON")
    if s and s.strip():
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"ACCOUNTS_JSON 不是合法 JSON: {e}。"
                "请确认使用英文双引号、无多余逗号、整体是一个数组 [ ... ]。"
            )

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            raise RuntimeError("ACCOUNTS_JSON 必须是非空数组，例如 [{\"username\":\"<账号>\",\"password\":\"<密码>\"}]")
        accounts = [_normalize_account(item, i + 1) for i, item in enumerate(data)]

        seen, uniq = set(), []
        for a in accounts:
            if a["username"] in seen:
                continue
            seen.add(a["username"])
            uniq.append(a)
        return uniq


    username = os.environ.get("USERNAME") or os.environ.get("ACCOUNT")
    password = os.environ.get("PASSWORD") or os.environ.get("PASS")
    if username and password:
        name = os.environ.get("NAME") or username
        return [{"name": name, "username": username.strip(), "password": password.strip()}]

    raise RuntimeError(
        "未提供账号。请在 Secrets 中配置 ACCOUNTS_JSON（推荐），"
        "或同时配置 USERNAME 与 PASSWORD。"
    )


def get_url():
    url = (
        os.environ.get("TARGET_URL")
        or os.environ.get("HOMEPAGE_URL")
        or os.environ.get("HOMEPAGE")
    )
    if not url or not url.strip():
        raise RuntimeError("未提供答题主页。请在 Secrets 中配置 TARGET_URL（或 HOMEPAGE_URL）。")
    return url.strip()
