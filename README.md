# tokenDiary - a simple dashboard for claude usage

A local, durable record of Claude Code token usage — and what it would have cost.

Assumptions for this project: you use Claude code locally(destop app, vscode, wsl, etc.), not by remote, not by APIs.

Claude Code logs every API call to local JSONL session files, then prunes them as they age. tokenDiary reads those files before they disappear and keeps a permanent record in SQLite and calculate the usage data. Cost is *notional* — what the usage would have cost at published API list rates, frozen at ingest so later price changes never rewrite history. A static dashboard shows a calendar heatmap, daily trends, and where the cost actually goes.

Python 3.11+, standard library only. No dependencies, no network, no build step.

---

## Core functions

### Scanning and storage

- data are scanned and stored to local SQLite, one row per API call.
- **Multiple sources** out of the box: local paths, WSL over `//wsl.localhost`, or any directory you can read and configure.

### Cost

- Priced from a versioned rate table, different model, thinking speed, cache read, input token, output token, they all matter for price.
- 5-minute and 1-hour cache writes are priced separately, the same way Anthropics did for their APIs.
- Unknown model → no cost recorded.

### Time

- Travel and time offset are picked up as long as the machine time is updated.

### Scheduling

- Daily run at-logon.
- Weekly-full run.

### Dashboard

- Calendar heatmap, stacked daily trend, stat tiles, and a table view of the same numbers.
- Filters: account, metric (notional cost, all tokens, tokens excluding cache reads, output tokens, or API calls), breakdown (account, model, or cost component), subagents, and date range.
- Today is drawn as provisional — it can still rise.
- Light and dark display mode.
- No CDN, works offline (locally)
- An optional **Refresh** button appears when the page is served by `python -m src serve`.

---

## Quick start

**Requirements:** Python 3.11 or newer — `tomllib` is the binding constraint. Developed
and run on 3.13. Nothing to install.

```bash
git clone <this repo> && cd tokenDiary
cp .env.example .env
```

Edit `.env` and fill in at least one source — an id and the path to that install's Claude
Code projects directory.

```
TD_S1_ID=laptop
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

Registers daily, at-logon, and weekly-full tasks. Pass `-Python` explicitly so a scheduled run is not pinned to whichever virtual environment happened to be active.

---

# 一个简单的 Claude 用量看板

Claude Code 会把每次 API 调用写入本地 JSONL 会话文件，并在文件变旧后自动清理。tokenDiary
在这些文件消失之前读取它们，并在 SQLite 中保留一份永久记录：每次 API 调用一行，按 message
ID 去重，可覆盖任意多台机器与账号。它从不写入 `.claude` 目录，也不会因为源文件消失而删除
任何一行。成本为**名义成本**——按官方 API 价目表计算的等价花费，在写入时冻结，之后调价不会
改写历史。静态看板提供日历热力图、每日趋势，以及成本的真实构成。

Python 3.11+，仅使用标准库。无依赖、无网络请求、无需构建。

## 核心功能

- **增量扫描**：用字节偏移和内容锚点跳过未变更的文件；48 小时内改动过的文件始终从头重读，
  因为活跃会话的前缀仍可能被改写。
- **数据持久化**：一次 API 调用一行，全局去重；token 计数只增不减，某一天的总量永远不会
  下降。Claude 清理源文件后，这些行依然保留并保留成本——这正是本项目存在的理由。
- **成本冻结**：价目表带版本号，改价不会重写历史；只有 `recost`（带 `--dry-run`）能改动
  已存成本。5 分钟与 1 小时缓存写入分别计价。
- **自动记录时区**：JSONL 只有 UTC，本地日期来自一份可追加的时区偏移历史。扫描发现机器
  时区变化时自动追加新时段，因此出差与夏令时都能被正确识别，也可手动修正。
- **定时任务**：`run` 一条命令完成 ingest + export 并写日志；每日、登录时、每周全量三个
  触发器。并发运行会干净地退出而不是互相破坏。
- **看板**：热力图、每日趋势、统计卡片、表格视图；可按账号、指标、分组维度、子代理、
  时间范围筛选；支持浅色/深色；由 `python -m src serve` 提供时会出现刷新按钮。

## 快速开始

```bash
cp .env.example .env     # 填入至少一个数据源的 id 与路径（用正斜杠）
python -m src scan       # 只读预览，不写入任何数据
python -m src run        # 采集 + 导出
python -m src serve      # 打开 http://127.0.0.1:8899
```

Windows 定时任务（可选）：

```bash
pwsh -NoProfile -File scripts/register-tasks.ps1 -Python C:/Python313/python.exe
```
