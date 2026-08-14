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

当前 CLI 提供真实任务的命令表面，但尚未接入生产 OpenAI-compatible factory 或配置 wiring。未传 `--mock-script` 时使用空的 `ScriptedMockLLM`，会安全暂停并报告 `script_exhausted`；这不是已运行的真实 LLM 结果。离线可重复运行应显式提供 mock script：

```text
cah run --workspace . --task "检查待处理的代码变更" --mock-script path/to/script.json
```

真实模型接入和凭据 wiring 仍属于后续工作，不能把上述命令写成已经访问在线服务。

所有 CLI 命令如下：

```text
cah run --workspace <path> --task "<task>" [--mock-script <path>]
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

命令组直接运行时会显示帮助。`cah report export` 生成的是本地报告；退出码反映最终会话状态。

## 凭据管理

使用凭据子命令管理当前 profile，录入和更新会提示输入，不把 secret 作为命令行参数：

```text
cah credentials set --profile default
cah credentials status --profile default
cah credentials update --profile default
cah credentials clear --profile default
```

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

## 静态 WebUI

`src/coding_agent_harness/web` 是可公开发布的静态会话报告预览，浏览器读取同目录的 `mock-report.json`。Pages 工作流先运行离线报告测试，再只复制这一目录并部署静态文件，不启动 Harness 后端、不执行 CLI、不连接 SQLite、不开放 WebSocket，也不提供审批或控制 API。生产 Pages 只允许 `main` push 或人工 dispatch 部署；功能分支和 PR 不覆盖生产站点。静态页面与 `cah` CLI 是两个明确边界：页面只展示脚本化 mock 数据，真实会话仍由本机 CLI 管理。

Repository/Pages URL placeholder: `<由负责人替换为真实仓库或 GitHub Pages URL>`。

## 安全边界

策略、审批、文件操作和验证器共同构成 CLI 的治理边界；默认拒绝远程 Git、网络和未批准的写操作。pytest 以当前 OS 用户权限执行，项目没有额外的 OS sandbox 或容器隔离，因此运行真实任务前应使用专用项目目录和低权限账户。审计数据库、备份和私有报告只应留在本机应用数据目录。

## 分发

构建 wheel 和 sdist：

```text
python -m build --no-isolation
```

产物位于 `dist/`，wheel 包含 `coding_agent_harness/web` 静态资源，sdist 可用于源码安装。课程交付 ZIP 应由负责人在确认 Git 跟踪内容后使用 `git archive --format=zip --output=<目标路径> HEAD` 生成，不包含 `.git`、Keyring、数据库、审计、备份、缓存或私有报告。

## 已知限制

- `cah run` 当前默认使用空 `ScriptedMockLLM` 并以 `script_exhausted` 暂停；生产 OpenAI-compatible factory/config wiring 尚未实现，不能据此声称真实模型已运行。
- Keyring 后端由操作系统提供，远程 CI 只运行离线测试，不读取个人凭据。
- 静态 WebUI 只展示固定 mock 报告，不是实时控制台，也不能审批、执行命令或访问本地数据库。
- Windows Python 3.13 是主要开发平台；Linux 3.13 是 GitHub/GitLab 的 CI 配置目标，需由远端运行后补充证据，本地文档不宣称 CI 已执行。
- Pages URL、仓库地址和远程 CI/Pages 运行结果必须由负责人以真实外部证据补充，本文不虚构远程状态。
- PR 合并前不会由功能分支产生新的生产 Pages 站点；这项生产安全约束优先于预合并在线预览。
