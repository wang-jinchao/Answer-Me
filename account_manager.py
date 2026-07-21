


import json
import sys
import shutil
import subprocess
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).resolve().parent / "accounts.json"


def load():
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[错误] 解析 accounts.json 失败: {e}")
        sys.exit(1)
    if not isinstance(data, list):
        print("[错误] accounts.json 顶层必须是数组 []")
        sys.exit(1)
    return data


def save(accounts):
    ACCOUNTS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[已保存] {ACCOUNTS_FILE}（{len(accounts)} 个账号）")


def mask(pwd):
    pwd = str(pwd)
    if len(pwd) <= 3:
        return pwd[0] + "**"
    return "*" * (len(pwd))


def cmd_list():
    accounts = load()
    if not accounts:
        print("(无账号，使用 `add` 添加，或用 `init` 创建模板)")
        return
    print(f"{'#':<4} {'姓名':<10} {'用户名':<14} {'密码'}")
    print("-" * 40)
    for i, a in enumerate(accounts, 1):
        print(f"{i:<4} {mask(a.get('name','')):<10} {mask(a.get('username','')):<14} {mask(a.get('password',''))}")


def cmd_show():
    if not ACCOUNTS_FILE.exists():
        print("[错误] 找不到 accounts.json，请先运行 init 或 add")
        sys.exit(1)
    content = ACCOUNTS_FILE.read_text(encoding="utf-8").strip()
    print("===== 复制以下内容（到 ===== 为止） =====")
    print(content)
    print("===== 复制结束 =====")


def cmd_add(name, username, password):
    accounts = load()
    if any(a.get("username") == username for a in accounts):
        print(f"[跳过] 用户名 {username} 已存在，未重复添加")
        return
    accounts.append({"name": name, "username": username, "password": password})
    save(accounts)
    print(f"[已添加] {name} ({username})")
    cmd_sync()


def cmd_remove(username):
    accounts = load()
    new = [a for a in accounts if a.get("username") != username]
    if len(new) == len(accounts):
        print(f"[未找到] 用户名 {username}")
        return
    save(new)
    print(f"[已删除] {username}")
    cmd_sync()


def cmd_init():
    if ACCOUNTS_FILE.exists():
        print("[已存在] accounts.json，未覆盖（如需重置请手动删除该文件）")
        return
    save([])
    print("[已创建] accounts.json（空模板，请用 add 添加账号）")


def cmd_sync(repo=None):
    if not ACCOUNTS_FILE.exists():
        print("[错误] 找不到 accounts.json，请先运行 init 或 add")
        sys.exit(1)




    if shutil.which("gh"):
        cmd = ["gh", "secret", "set", "ACCOUNTS_JSON"]
        if repo:
            cmd += ["--repo", repo]
        print("[同步] 正在推送到 GitHub Secret: ACCOUNTS_JSON ...")
        try:
            content = ACCOUNTS_FILE.read_text(encoding="utf-8")
            subprocess.run(cmd, input=content, text=True, check=True)
            print("[完成] Secret 已更新。GitHub Action 下次运行将使用最新账号列表。")
            return
        except subprocess.CalledProcessError as e:
            print(f"[警告] gh 执行失败（{e}），已回退到手动粘贴方式。")


    content = ACCOUNTS_FILE.read_text(encoding="utf-8").strip()
    print("[手动同步] 未使用 gh，请把下面 ===== 之间的内容整段复制：")
    print("===== 复制开始 =====")
    print(content)
    print("===== 复制结束 =====")
    print("然后到 GitHub 网页完成粘贴：")
    print("  1. 打开你的仓库 → Settings → Secrets and variables → Actions")
    print("  2. 新建/编辑 Secret，名称填：ACCOUNTS_JSON")
    print("  3. 把上面内容粘贴进去，保存即可。")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    op = args[0]
    if op == "list":
        cmd_list()
    elif op == "show":
        cmd_show()
    elif op == "add" and len(args) == 4:
        cmd_add(args[1], args[2], args[3])
    elif op == "remove" and len(args) == 2:
        cmd_remove(args[1])
    elif op == "init":
        cmd_init()
    elif op == "sync":
        repo = args[2] if len(args) >= 3 and args[1] == "--repo" else None
        cmd_sync(repo)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
