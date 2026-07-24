# Answer-Me

自动完成 upfitapp.com 每日任务（每日一学 / 每日一看 / 每日一练 / 每日一答，**以及可选的每日一走步数同步**）并汇总积分报告。

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
    ├── report.py                 # JSON + Markdown 报告（含总览判定）
    └── walk.py                   # 每日一走 liteapp 步数同步（openId 体系，可选任务）
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

**每日一走（可选，仅特定账号需要）：** 在对应账号对象里追加以下**静态字段**即可启用，未配置的账号自动跳过、不影响答题。
```
ACCOUNTS_JSON = [{
  "name":"王晋超","username":"<账号>","password":"<密码>",
  "openid":"<微信openId>", "unionid":"<微信unionid>",
  "walk_name":"王晋超",
  "walk_step": 11000
}]
```
- 每日一走走**微信小程序 liteapp 接口（openId 体系）**，与 Web 答题是两套体系。
- **微信运动加密包（enc/iv/key）不放在 ACCOUNTS_JSON**——它是会话级动态数据，每次从微信小程序实时请求抓取，由**运行时环境变量 `WALK_ENC` / `WALK_IV` / `WALK_KEY`** 注入（Secret 或 workflow 环境变量）。写死在静态配置既无意义又很快失效。未注入时 `run_walk` 标 `skipped`（不写步数），不影响其他任务。
- **执行顺序**：先 `run_walk` 写步数 → 再读 histscore 取今日「每日运动 + 步数达标」求和（**正常 +10**），写入报告便于核对。
- **步数规则**：每次运行**只上传当天这一条**记录（`timestamp` = 当天东八区 0 点秒级、`step` = 目标步数）；步数落在 **[10000, 12000] 随机**（配置 `walk_step` 在该区间内才生效，否则随机）；不在意后端对历史是合并还是覆盖，只要今天这条传上即可。
- **不验证写入结果**：uploadstep 调用后即标记完成（不强制复核落库），符合"只同步、不校验"诉求。
- **绑定校验只用 `walk_name`**：写前 `index.userName` 必须含 `walk_name` 才写，否则**立即中止、绝不误写**到错误账号（与账号显示名 `name` 无关）。
- 静态 walk 字段（openid/unionid/walk_name/walk_step）只放 Secret，绝不写进仓库代码；动态 enc/iv/key 走独立 Secret。

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
