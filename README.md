# tokenDiary - a simple dashboard for Claude usage

A local, durable record of Claude Code token usage — and what it would have cost.

**What this assumes.** You use Claude Code on this machine — the desktop app, VS Code, a terminal, WSL. tokenDiary only reads the session files Claude Code writes to disk, so what it can show you is exactly what those files contain.

It will not see usage from claude.ai in the browser, the Claude chat apps, Claude Code running in the cloud, or your own scripts calling the Anthropic API. None of those leave session files on your machine.

Sessions you run over SSH *are* counted. Claude Code writes a second copy on the machine you connect from, and tokenDiary recognises the duplicate and counts the call once.

Claude Code logs every API call to local JSONL session files, then prunes them as they age. tokenDiary reads those files before they disappear, keeps a permanent record in SQLite, and works out what the usage cost. Cost is *notional* — what it would have cost at published API list rates, frozen when recorded so later price changes never rewrite history. A static dashboard shows a calendar heatmap, daily trends, and where the cost actually goes.

Python 3.11+, standard library only. No dependencies, no network, no build step.

---

## Core functions

### Scanning and storage

- data are scanned and stored to local SQLite, one row per API call.
- **Multiple sources** out of the box: local paths, WSL over `//wsl.localhost`, or any directory you can read and configure.

### Cost

- Priced from a versioned rate table. Model, speed (fast mode costs double on some models), cache reads, input and output tokens all change the price.
- 5-minute and 1-hour cache writes are priced separately, the same way Anthropic prices them.
- Thinking tokens are not billed on top — they are already part of output tokens.

### Time

- Travel and time offset are picked up as long as the machine time is updated.

### Scheduling

Three Windows tasks, registered in one step (see below):

- **Daily at 21:00** — a quick incremental scan.
- **At logon** — catches up if the machine was off or you were signed out at 21:00.
- **Weekly, Sunday at 20:00** — a full re-read of every file, so nothing can be missed by the incremental shortcut.

Times are defaults you can change (`-DailyAt`, `-WeeklyAt`, `-WeeklyOn`). Tasks run only while you are logged in — a locked screen still counts, signing out does not.

**This part is Windows-only.** On macOS or Linux, schedule it yourself. `run` is a single
command that reports success or failure through its exit code, so a crontab line is enough:

Put these in `crontab -e`:

```cron
0 21 * * *  cd /path/to/tokenDiary && /usr/bin/python3 -m src run
0 20 * * 0  cd /path/to/tokenDiary && /usr/bin/python3 -m src run --full
@reboot     cd /path/to/tokenDiary && /usr/bin/python3 -m src run
```

Daily at 21:00, a full re-read on Sundays at 20:00, and a catch-up at boot. That last line
stands in for the Windows at-logon task: unlike Task Scheduler, cron does **not** run a job
it missed, so without it a machine that was off at 21:00 simply skips that day.

### Dashboard

- Calendar heatmap, stacked daily trend, stat tiles, and a table view of the same numbers.
- Filters: account, metric (notional cost, all tokens, tokens excluding cache reads, output tokens, or API calls), breakdown (account, model, or cost component), subagents, and date range.
- Today is drawn as provisional — it can still rise.
- Light and dark display mode.
- No CDN, works offline (locally)
- An optional **Refresh** button appears when the page is served by `python -m src serve`.

---

## Quick start

**Requirements:** Python 3.11 or newer. Nothing to install.

| | Windows | macOS / Linux |
|---|---|---|
| Scanning, storage, pricing, dashboard | yes | yes |
| Automatic scheduling | one script, below | cron or launchd |
| WSL sources over `//wsl.localhost` | yes | not applicable |

Only the scheduling helper is platform-specific, nothing else in the code is.

```bash
git clone <this repo> && cd tokenDiary
cp .env.example .env
```

Edit `.env` and fill in at least one source — an id and the path to that install's Claude
Code projects directory.

```
TD_S1_ID=account1
TD_S1_PATH=C:/Users/<you>/.claude/projects
```

Use forward slashes everywhere, including WSL paths
(`//wsl.localhost/<Distro>/home/<you>/.claude/projects`).

Then:

```bash
python -m src scan      # read-only preview -- parses and reports, writes nothing
python -m src run       # ingest + export (the everyday command)
python -m src serve     # dashboard at http://127.0.0.1:8899
```

**Schedule it (Windows, optional):**

```bash
pwsh -NoProfile -File scripts/register-tasks.ps1 -Python <your python.exe interpreter path>
```

Registers all three tasks. Pass `-Python` explicitly so a scheduled run is not pinned to whichever virtual environment happened to be active when you ran this.

To use different times:

```bash
pwsh -NoProfile -File scripts/register-tasks.ps1 -DailyAt 09:00 -WeeklyOn Saturday
```

---

# tokenDiary - 一个简单的 Claude 用量看板

在本地长期保存 Claude Code 的 token 用量记录，并算出它值多少钱。

**使用前提。** 你在本机使用 Claude Code —— 桌面端、VS Code、终端或 WSL 均可。tokenDiary
只读取 Claude Code 写在磁盘上的会话文件，因此它能呈现的，就是这些文件里已有的内容。

它看不到这些用量：浏览器里的 claude.ai、Claude 聊天应用、云端运行的 Claude Code，以及你
自己调用 Anthropic API 的脚本。它们都不会在本机留下会话文件。

通过 SSH 运行的会话**会**被统计。Claude Code 会在你发起连接的那台机器上再写一份副本，
tokenDiary 能识别出这份重复记录，只计一次。

Claude Code 会把每次 API 调用写入本地 JSONL 会话文件，并在文件变旧后自动清理。tokenDiary
在这些文件消失之前读取它们，在 SQLite 中保留一份永久记录，并算出这些用量的花费。成本为
**名义成本**——按官方 API 价目表计算的等价花费，在记录时即冻结，之后调价不会改写历史。
静态看板提供日历热力图、每日趋势，以及成本的真实构成。

Python 3.11+，仅使用标准库。无依赖、无网络请求、无需构建。

---

## 核心功能

### 扫描与存储

- 数据扫描后存入本地 SQLite，每次 API 请求对应一行数据。
- **支持多个数据源**：本地路径、通过 `//wsl.localhost` 访问的 WSL，或任何你能读取并配置的
  目录。

### 成本

- 按带版本号的价目表计价。模型、速度模式（部分模型的 fast 模式价格翻倍）、缓存读取、输入与
  输出 token，都会影响价格。
- 5 分钟与 1 小时缓存写入分别计价，与 Anthropic 官方的计价方式一致。
- 思考（thinking）token 不额外计费——它本来就包含在输出 token 之中。

### 时间

- 只要机器时间保持更新，出差与时区变化都会被自动记录。

### 定时任务

三个 Windows 计划任务，一步注册完成（见下文）：

- **每天 21:00** —— 快速增量扫描。
- **登录时** —— 如果 21:00 时机器关着或你已注销，这次会补上。
- **每周日 20:00** —— 完整重读所有文件，确保增量扫描不会漏掉任何内容。

以上时间均为默认值，可通过 `-DailyAt`、`-WeeklyAt`、`-WeeklyOn` 修改。任务只在你处于登录
状态时运行 —— 锁屏仍算登录，注销则不算。

**这部分仅限 Windows。** 在 macOS 或 Linux 上请自行安排定时任务。`run` 是一条完整的命令，
并通过退出码汇报成功或失败，因此一行 crontab 就够了：

在 `crontab -e` 中写入：

```cron
0 21 * * *  cd /path/to/tokenDiary && /usr/bin/python3 -m src run
0 20 * * 0  cd /path/to/tokenDiary && /usr/bin/python3 -m src run --full
@reboot     cd /path/to/tokenDiary && /usr/bin/python3 -m src run
```

即每天 21:00 增量扫描、每周日 20:00 完整重读，以及开机时补跑一次。最后一行对应 Windows 的
"登录时"任务：与 Task Scheduler 不同，cron **不会**补跑错过的任务，因此没有这一行的话，
21:00 时处于关机状态的那一天就会被直接跳过。

### 看板

- 日历热力图、每日堆叠趋势、统计卡片，以及同一份数据的表格视图。
- 筛选维度：账号、指标（名义成本、全部 token、不含缓存读取的 token、输出 token、调用次数）、
  分组方式（账号、模型或成本构成）、子代理、时间范围。
- 当天以"未完成"样式绘制 —— 它还可能继续上涨。
- 支持浅色与深色模式。
- 不依赖 CDN，可离线（本地）使用。
- 当页面由 `python -m src serve` 提供时，会出现一个可选的**刷新**按钮。

---

## 快速开始

**环境要求：** Python 3.11 或更高版本，无需安装任何依赖。

| | Windows | macOS / Linux |
|---|---|---|
| 扫描、存储、计价、看板 | 支持 | 支持 |
| 自动定时任务 | 一个脚本，见下文 | cron 或 launchd |
| 通过 `//wsl.localhost` 读取 WSL | 支持 | 不适用 |

只有定时任务脚本与平台相关,代码其余部分都没有平台限制。

```bash
git clone <本仓库> && cd tokenDiary
cp .env.example .env
```

编辑 `.env`，至少填入一个数据源：一个 id，以及该 Claude Code 安装的 projects 目录路径。

```
TD_S1_ID=account1
TD_S1_PATH=C:/Users/<你>/.claude/projects
```

所有路径都请使用正斜杠，WSL 路径也一样
（`//wsl.localhost/<发行版>/home/<你>/.claude/projects`）。

然后：

```bash
python -m src scan      # 只读预览 —— 解析并汇报，不写入任何数据
python -m src run       # 采集 + 导出（日常使用的命令）
python -m src serve     # 看板地址 http://127.0.0.1:8899
```

**设置定时任务（Windows，可选）：**

```bash
pwsh -NoProfile -File scripts/register-tasks.ps1 -Python <你的 python.exe 路径>
```

一次注册上述三个任务。请显式传入 `-Python`，以免定时任务被固定在你当时恰好激活的某个虚拟
环境上。

如需修改时间：

```bash
pwsh -NoProfile -File scripts/register-tasks.ps1 -DailyAt 09:00 -WeeklyOn Saturday
```
