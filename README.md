# Coding Agent Harness Jie

## 项目简介

Coding Agent Harness 是一个 Python 3.13 项目，用于在本地以可审计、可批准的方式运行编码代理。工具调用经过策略判断、审批和结果验证，状态与审计记录保存在应用数据目录中。默认演示使用离线 mock，不需要网络或真实凭据。

## 安装

Windows 推荐使用 Python 3.13；Linux 的同一套命令用于 CI。开发安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

当前优先构建并安装本地 wheel，这条路径不依赖尚未发布的公共包：

```text
python -m build --no-isolation
python -m pip install dist/coding_agent_harness_jie-0.1.0-py3-none-any.whl
pipx install dist/coding_agent_harness_jie-0.1.0-py3-none-any.whl
```

发布到受信任索引后，才使用包名安装：

```text
python -m pip install coding-agent-harness-jie
pipx install coding-agent-harness-jie
```

离线源码安装需要先准备构建依赖。CI 使用 `python -m pip install ".[dev]"`，然后按 pytest、Ruff、严格 mypy、build 的顺序验证。

## 运行

安装后入口命令为 `cah`，也可以使用 `python -m coding_agent_harness.cli`。先运行离线治理演示：

```text
cah demo governance
```

未传 `--mock-script` 时，`cah run` 会读取分层配置和操作系统 Keyring，创建
`OpenAICompatibleClient` 并调用兼容的 `/chat/completions` 原生 tool-calling 接口。先配置凭据，再运行真实任务：

```text
cah credentials set --profile default
cah run --workspace . --task "修复当前失败的测试"
```

离线可重复运行应显式提供 mock script。mock 模式不会读取 Keyring；脚本动作耗尽时会安全暂停并报告 `script_exhausted`：

```text
cah run --workspace . --task "检查待处理的代码变更" --mock-script path/to/script.json
```

本仓库没有使用真实供应商凭据执行 smoke test，不能把离线测试结果表述成真实模型效果。供应商必须兼容 OpenAI chat completions 的原生工具调用结构；不兼容该协议时客户端会 fail-closed。

所有 CLI 命令如下：

```text
cah run --workspace <path> --task "<task>" [--mock-script <path>] [预算收紧选项]
cah sessions list
cah sessions show <session-id>
cah sessions resume <session-id>
cah approvals list [--session-id <session-id>]
cah approvals approve <approval-id> [--yes]
cah approvals deny <approval-id>
cah changes show <session-id>
cah changes keep <session-id>
cah changes rollback <session-id> [--yes]
cah credentials status [--profile <profile>]
cah credentials set [--profile <profile>]
cah credentials update [--profile <profile>]
cah credentials clear [--profile <profile>]
cah memory list <project-id>
cah memory approve <entry-id>
cah memory reject <entry-id>
cah memory delete <entry-id>
cah report export <session-id> <output>
cah demo governance
```

预算收紧选项包括 `--max-steps`、`--max-llm-calls`、`--max-consecutive-failures`、
`--max-repeated-action`、`--command-timeout-seconds`、`--session-timeout-minutes` 和
`--max-observation-bytes`。这些参数只能降低较高信任层给出的上限。命令组直接运行时会显示帮助。
`cah report export` 生成的是本地报告；退出码反映最终会话状态。

## 凭据管理

使用凭据子命令管理当前 profile，录入和更新会提示输入，不把 secret 作为命令行参数：

```text
cah credentials set --profile default
cah credentials status --profile default
cah credentials update --profile default
cah credentials clear --profile default
```

## 分层配置

可信用户配置位于 Windows `%LOCALAPPDATA%\CodingAgentHarness\config.toml`，非 Windows 位于
`~/.local/share/CodingAgentHarness/config.toml`。配置只保存供应商地址、模型名、凭据 profile
和预算，不保存 API Key：

```toml
provider_url = "https://api.openai.com/v1"
model = "gpt-5-mini"
credential_profile = "default"

[budgets]
max_steps = 20
max_llm_calls = 12
command_timeout_seconds = 120
```

目标仓库可以创建 `harness.toml`，声明源码根、验证器和更严格的预算：

```toml
source_roots = ["src", "tests"]

[[validators]]
validator_id = "pytest"
program = "python"
args = ["-m", "pytest", "-q"]
stages = ["baseline", "fast", "final"]
required = true

[budgets]
max_steps = 10
max_llm_calls = 8
```

信任顺序为内置硬上限、用户配置、项目配置、CLI/会话参数；后三层只能逐层收紧。项目验证器必须
同时通过内置命令策略，shell、网络工具、越界路径和带写副作用的危险参数会在创建会话前被拒绝。

## 凭据安全

首选操作系统 Keyring；本项目的应用数据路径为 Windows `%LOCALAPPDATA%\CodingAgentHarness`，非 Windows 为 `~/.local/share/CodingAgentHarness`，其中包含 `state.db`、`audit/events.jsonl`、`locks/` 和 `backups/`。确需环境变量时，只在当前进程或受控 CI secret store 中提供，并避免把 `.env`、token、私钥和导出的审计数据加入 Git。

Keyring 和环境变量都由运行账户的权限保护，不能替代操作系统账户隔离。不要在 issue、日志、报告或截图中粘贴真实 API key。此仓库的测试、mock demo 和 Pages 工作流不读取 `.env`、真实 Keyring、真实 API key 或真实 LLM。

## 目录结构

```text
src/coding_agent_harness/  核心 Python 包和 CLI
src/coding_agent_harness/web/  只读静态 WebUI 与 mock-report.json
tests/  离线单元与集成测试
scripts/verify.ps1          Windows 本地质量门禁
scripts/verify.sh           Linux/macOS shell 质量门禁
.github/workflows/          GitHub CI 与静态 Pages
.gitlab-ci.yml              GitLab unit-test 与分发 artifact
```

## 第三方依赖与许可证

项目没有复制第三方项目源码；以下直接依赖通过 Python 包管理器安装，版权和许可证归各自项目所有，
具体文本以安装包附带的许可证为准。

| 用途 | 依赖 | 许可证 |
|---|---|---|
| HTTP 客户端 | httpx | BSD-3-Clause |
| 系统凭据存储 | keyring | MIT |
| 严格数据模型 | pydantic | MIT |
| CLI | typer | MIT |
| 构建 | build、hatchling | MIT |
| 测试与覆盖率 | pytest、pytest-cov | MIT |
| 类型与静态检查 | mypy、ruff | MIT |
| CI 配置解析测试 | PyYAML | MIT |

## 静态 WebUI

`src/coding_agent_harness/web` 是可公开发布的静态会话报告预览，浏览器读取同目录的 `mock-report.json`。Pages 工作流先运行离线报告测试，再只复制这一目录并部署静态文件，不启动 Harness 后端、不执行 CLI、不连接 SQLite、不开放 WebSocket，也不提供审批或控制 API。生产 Pages 只允许 `main` push 或人工 dispatch 部署；功能分支和 PR 不覆盖生产站点。静态页面与 `cah` CLI 是两个明确边界：页面只展示脚本化 mock 数据，真实会话仍由本机 CLI 管理。

Repository: <https://github.com/jiezhouziheng/Coding_Agent_Harness_Jie>

GitHub Pages: <https://jiezhouziheng.github.io/Coding_Agent_Harness_Jie/>

## 安全边界

策略、审批、文件操作和验证器共同构成 CLI 的治理边界；默认拒绝远程 Git、网络和未批准的写操作。pytest 以当前 OS 用户权限执行，项目没有额外的 OS sandbox 或容器隔离，因此运行真实任务前应使用专用项目目录和低权限账户。审计数据库、备份和私有报告只应留在本机应用数据目录。

## 分发

构建 wheel 和 sdist：

```text
python -m build --no-isolation
```

产物位于 `dist/`，wheel 包含 `coding_agent_harness/web` 静态资源，sdist 可用于源码安装。课程交付 ZIP 应由负责人在确认 Git 跟踪内容后使用 `git archive --format=zip --output=<目标路径> HEAD` 生成，不包含 `.git`、Keyring、数据库、审计、备份、缓存或私有报告。

## 已知限制

- 生产组装已接入 OpenAI-compatible 客户端、分层 TOML 与 Keyring，但只实现原生 chat completions tool calling；本项目未用真实供应商执行 smoke test，不对所有兼容供应商作效果或协议一致性承诺。
- `--mock-script` 是离线确定性入口；脚本动作耗尽会以 `script_exhausted` 暂停，不代表真实模型结果。
- Keyring 后端由操作系统提供，远程 CI 只运行离线测试，不读取个人凭据。
- 静态 WebUI 只展示固定 mock 报告，不是实时控制台，也不能审批、执行命令或访问本地数据库。
- Windows Python 3.13 是主要开发平台；Linux 3.13 是 GitHub/GitLab 的 CI 配置目标，其中 GitHub Actions 的 Python 3.13 `unit-test` 已真实通过。GitLab 使用同一命令合同，但本项目未声明已在 GitLab runner 上执行。
- 真实 PR、CI、artifact、Pages 和课程 ZIP 证据记录在 `PLAN.md`、`AGENT_LOG.md` 与已合并的 PR #4 中；精确 ZIP 散列保留在外部交付证据中以避免文档自引用改变归档散列。
- PR 合并前不会由功能分支产生新的生产 Pages 站点；这项生产安全约束优先于预合并在线预览。
