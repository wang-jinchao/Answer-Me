# Answer-Me

自动完成 upfitapp.com 每日任务（每日一学 / 每日一看 / 每日一练 / 每日一答）并汇总积分报告。

基于 **Python + Playwright**，在 GitHub Actions 上运行，由 **cron-job.org** 精确触发定时任务。

## 目录结构

```
.
├── .github/workflows/daily.yml   # 工作流：被 cron-job.org 触发后跑代码
├── .gitignore                    # 必须包含 accounts.json
├── requirements.txt              # playwright + pycryptodome
├── account_manager.py            # 本地多账号管理 + 同步到 Secret
├── accounts.json                 # 本地账本（被 gitignore，切勿提交）
└── src/
    ├── config.py                 # 从 Secrets/环境变量读账号与目标地址
    ├── utils.py                  # 验证码 HMAC 穷举 + AES 兜底
    ├── browser.py                # Playwright 启动 + 每账号独立隔离
    ├── account_runner.py         # 登录 + 四环节 + 单账号循环（L685 = 登录失败）
    ├── main.py                   # 入口：读账号 → 并发跑 → 生成报告
    └── report.py                 # JSON + Markdown 报告（含总览判定）
```

## 1. 配置 Secrets

在仓库 `Settings → Secrets and variables → Actions` 中配置：

**多账号（推荐）：**
```
ACCOUNTS_JSON = [{"name":"<姓名>","username":"<账号>","password":"<密码>"}]
TARGET_URL    = <首页地址>
```

**单账号（备选）：**
```
USERNAME     = <账号>
PASSWORD     = <密码>
NAME         = 张三            # 可选显示名
HOMEPAGE_URL = <首页地址>
```

> ⚠️ **字段名必须是 `username` / `password`**（别名 `user`/`account`、`pass`/`pwd` 也接受）。写错会导致取到空值、登录失败，报错 `account_runner.py#L685 Login could not be confirmed`。

## 2. 本地管理账号（推荐做法）

GitHub Secrets 是**只写不可回看**的——设过之后你没法再看到内容。因此用本地 `accounts.json` 当唯一账本：

```bash
python account_manager.py init                        # 创建空 accounts.json
python account_manager.py add <姓名> <账号> <密码>      # 添加（装了 gh 会自动同步 Secret）
python account_manager.py list                        # 查看账号（密码脱敏）
python account_manager.py remove <账号>              # 删除（自动同步）
python account_manager.py show                        # 打印原始 JSON 供手动复制
python account_manager.py sync                        # 手动推送到 Secret
```

- 装了 `gh` CLI 并 `gh auth login` 时，`add`/`remove`/`sync` 会自动调用 `gh secret set` 推送。
- 没装 `gh` 时，脚本会打印 JSON，你复制后粘进 GitHub 的 `ACCOUNTS_JSON` Secret 即可。
- **`accounts.json` 已在 `.gitignore` 中，切勿 `git add` 提交**（fork 也拿不到它）。

## 3. 定时触发（cron-job.org）

GitHub 自带的 `schedule` 在**开源（public）仓库**上常被排队延迟数小时。本仓库已移除它，改用 **[cron-job.org](https://cron-job.org)**（免费、高精度）在指定时刻向 GitHub API 发请求，触发 `workflow_dispatch`。

代码与账号密码始终留在 GitHub，cron-job.org 只发送一个"开始执行"的信号。

### 3.1 创建细粒度 PAT

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate：
- Resource owner：`wang-jinchao`
- Repository access：**Only select repositories** → 勾 `Answer-Me`
- Permissions → **Actions: Read and write**（仅此一项，足够触发 workflow）
- 复制令牌（形如 `github_pat_...`）

> 该令牌仅能写这一个仓库的 Actions，且存于第三方 cron-job.org，建议定期轮换、不用时 revoke。

### 3.2 在 cron-job.org 创建任务

1. 注册并 **Create Cronjob**
2. **Address (URL)**：
   ```
   https://api.github.com/repos/wang-jinchao/Answer-Me/actions/workflows/daily.yml/dispatches
   ```
3. **Request method**：`POST`
4. **Request headers**（逐条添加）：
   ```
   Authorization: Bearer <你的PAT>
   Accept: application/vnd.github+json
   User-Agent: cron-job.org
   ```
5. **Request body**（raw）：`{"ref":"main"}` ← 你的默认分支名（一般是 `main`）
6. **Timezone**：`Asia/Shanghai`；**Schedule**：Daily，**09:00**（任意时间均可）
7. 保存

> 保存后到 GitHub 仓库 **Actions** 标签验证：到点会准时出现一条 "Daily Automation" 运行记录即配置成功。

## 4. 运行报告

每次运行结束会生成报告，可通过两种方式查看：

- **Actions 运行页 → Summary 标签**：直接看到 `✅ 全部完成` / `❌ 有任务未完成` 的总览判定 + 各账号速览（无需下载）。
- **Artifacts → reports**：下载 `reports/<日期>.md` / `.json` 看逐项明细。

报告判定口径：
- `done` = 完成 ✅
- `no_attempts` / `skipped` = 中性跳过 ⚪（如"今日已无次数"）
- `failed` / 结果缺失 = 失败 ❌

## 5. 多账号并发

`daily.yml` 的 `env` 里 `MAX_WORKERS: "2"` 控制最多同时跑几个账号：
- 设 `1` = 完全顺序执行（最稳，降低同 IP 风控风险）
- 设更高 = 更快但并发更高

仅当 `ACCOUNTS_JSON` 里有 ≥3 个账号时该值才有区别。

## 常见问题

- **L685 Login could not be confirmed**：通常是 `ACCOUNTS_JSON` 字段名写错（应为 `username`/`password`）。用 `account_manager.py` 在配置期即可校验出来。
- **想给 GitHub 自带定时留个备份**：取消 `daily.yml` 中注释掉的 `schedule:` 块即可（任务幂等，当天做过了会自动跳过，重复跑不影响计分）。
- **报告看不到**：先看 Actions 运行页的 **Summary** 标签；需要细节再下 **Artifacts → reports**。
