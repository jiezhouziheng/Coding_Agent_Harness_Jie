# Coding Agent Harness Jie - 规约形成过程

> 状态：已完成；brainstorming、实施计划、四个 PR、合并后 CI/Pages 与最终交付验证均有真实证据
>
> 负责人：Jie
>
> 当前记录时间范围：2026-08-09 至 2026-08-11
>
> 主开发智能体：OpenAI Codex App + Superpowers 5.1.3

## 1. 文档目的

本文件记录项目规约与计划如何形成、哪些建议来自 AI、哪些决定由项目负责人采纳、推翻或修正，以及什么证据导致设计发生变化。

`SPEC.md` 是规范性的产品行为合同；本文件保存推理、对话和过程证据。它是一份持续更新的文档：brainstorming 与计划部分已经完成；外部 Claude Code 实现试运行未执行，改用 fresh Codex 受限上下文只读审查，二者的证据强度差异在本文件中明确说明。

## 2. 初始背景与约束

项目开始时，负责人明确了以下边界：

- 详细读取目录中的全部 Markdown 说明，不读取 PDF 副本。
- 未经明确许可，不修改文件或生成实现内容。
- 使用 Python，因为开发便捷性高。
- 使用 GitHub 托管开发历史，最终通过课程网站提交源码和文档 ZIP。
- 六个维度均需可运行，并把治理作为深入维度。
- 可用时间仅为 2026-08-10 至 2026-08-13，范围必须严格控制。

仓库最初只有两行 `README.md` 和 `.gitignore`。在只读阶段核对了 Git、远端、Conda/Python 和插件。Superpowers 5.1.3 已包含 brainstorming、writing-plans、TDD、worktree、subagent、code review 和完成验证等技能。项目当前使用 Conda base，Python 3.13.11。

两份课程 Markdown 被拼接成完整要求。最重要的实现边界是：交付物必须包含项目自己编码的 agent 主循环；移除真实 LLM 后，治理、工具、反馈、记忆、配置和停机仍然能够确定性测试。

## 3. Superpowers Brainstorming 的使用方式

在任何代码或项目骨架生成前调用了 `superpowers:brainstorming`，过程如下：

1. 检查课程要求、仓库、Git、环境和可用 skills。
2. 提议并获得许可使用 visual companion。
3. 每次只提出一个决策问题，通常给出三种方案、取舍和推荐。
4. 依次锁定目的、范围、成功标准、安全边界和交付约束。
5. 比较三种完整系统架构。
6. 按产品范围、架构、治理、数据隐私、主循环和交付逐节展示设计并获得批准。
7. 使用课程指定的根目录 `SPEC.md`，覆盖 Superpowers 默认的 `docs/superpowers/specs/...` 路径。
8. 对写入后的 SPEC 做自审，再交由负责人审阅；只有批准后才能调用 `writing-plans`。

Brainstorming 期间没有调用实现技能，也没有编写实现代码。

## 4. 决策时间线

| 主题 | 比较的方案 | 人工决定 | 对设计的影响 |
|---|---|---|---|
| 主要任务 | 修复失败测试；实现功能；仓库维护 | 失败测试为主，功能为辅，维护必须体现 | `pytest` 成为核心客观反馈 |
| 语言范围 | 仅 Python；Python-first 可扩展；完整多语言 | Python-first + 可配置验证器 | 第一版不做多语言插件平台 |
| 仓库输入 | 本地路径；上传 ZIP；Git clone/push | 指定已有本地仓库 | 建立工作区围栏，不处理远程 Git 凭据 |
| 深入维度 | 六维平均；治理深入 | 治理深入 | 中央策略、持久化 HITL、预算、审计、回滚 |
| 风险体验 | 每次写入审批；三级风险；只输出 patch | 三级风险 | `ALLOW / REQUIRE_APPROVAL / DENY` 成为代码判定 |
| 用户界面 | 完整 WebUI；纯 CLI；CLI + 最小 WebUI | CLI 主控 + 静态只读报告页 | WebUI 不具备控制能力 |
| 审批体验 | 当前终端询问；延迟审批；两者结合 | 立即询问 + 持久化待审批 | 支持跨退出恢复，并要求防重放 |
| LLM 接入 | 单一厂商；OpenAI-compatible；只用 mock | OpenAI-compatible + mock | 一个低层适配器，不使用 agent SDK |
| 凭据 | OS Keyring；加密文件；仅 `.env` | OS Keyring | 隐藏录入和假测试后端 |
| 记忆 | 手工备注；完整历史；受治理结构化记忆 | 受治理结构化记忆 | 候选、证据、批准和有界检索 |
| 配置 | 项目控制全部；分层信任；仅 CLI | 分层信任 | 低信任配置不能扩大权限 |
| 文件修改 | 精确替换；unified diff；shell | 精确替换 | 确定性匹配、原子写入、明确错误 |
| 命令 | 固定验证器；结构化子进程；原始 shell | 白名单 `shell=False` 子进程 | 参数可独立治理，行为更确定 |
| 反馈 | 仅 `pytest`；分阶段可配置；每次全量 | Python 默认 + 项目扩展 | 基线、快速反馈和完成门禁 |
| 失败后的修改 | 全部保留；自动回滚；用户选择 | 日志 + 保留/回滚 | 不依赖 `git reset` 的可逆修改 |
| 运行预算 | 很小；平衡默认值；无限/全配置 | 平衡且有上限 | 达到限制时暂停，不误报成功 |
| 持久化 | 多 JSON；SQLite 混合；事件溯源 | SQLite + 文件备份 + JSONL | 事务状态、恢复和可读审计 |
| LLM 动作协议 | 原生 tool calls；JSON 文本；自然语言解析 | 原生 tool calls + Pydantic | 一次纠正，第二次失败暂停 |
| 上下文 | 全仓/全历史；有界按需；向量检索 | 有界上下文 + 工具读取 | 不发送完整仓库，每轮最多 5 条记忆 |
| 分发 | Python 包 + 静态报告；Docker；完整 Web 服务 | Python 包 + GitHub Pages mock | 真实 CLI 适配主机 Keyring，网页无控制面 |
| UI 设计 | 不用设计工具；完整前端；最小 Open Design | `web-prototype + Neutral Modern` | Open Design 只服务报告页 |
| 整体架构 | 中央策略循环；批量 patch；事件插件平台 | 中央策略网关循环 | 唯一治理入口和细粒度反馈 |

## 5. 关键迭代与对话证据

### 迭代 1：把不清楚的项目变成可测试产品

负责人明确表示：

> “我没有相关的项目经验，还是不太清楚应该做什么。”

因此，智能体没有直接生成项目，而是开始逐题 brainstorming。首先比较“修复失败测试、实现小功能、仓库维护”三种任务形态。负责人的决定是：

> “我认为A确实适合，B都可以作为次要能力，C当然也要有所体现。”

迭代前，产品只有“Coding Agent Harness”这一宽泛名称。迭代后，主场景被收敛为修复 Python `pytest` 失败；只有存在客观验证时才做小功能，并纳入 lint/type/test 维护。这为反馈闭环提供了确定性信号，也避免产品成为普通聊天封装。

### 迭代 2：解决 CLI 与 WebUI 要求冲突

课程通用 Markdown 一处说明纯 CLI/纯后端可以豁免前端，最终清单另一处又把 WebUI 写成必交项。

智能体最初建议“CLI 为主 + 最小只读 WebUI”。由于时间紧，负责人先决定：

> “考虑到时间紧张，采用CLI吧。”

智能体随后过度强调最终清单中的 WebUI。负责人质疑是否真的读过两份 Markdown，并报告纯 CLI 已被允许。智能体重新定位了矛盾行并接受纯 CLI 边界。

进一步独立核对后，负责人再次修正决定：

> “为了避免不必要的失分，我决定采用你的‘CLI 为主 + 最小只读 WebUI’方案。”

最终设计不是带执行能力的 Web 应用：CLI 是完整控制面，WebUI 只是读取脱敏导出 JSON 的静态报告页。这一轮充分体现 human-owned：负责人两次挑战 AI，独立核对评分要求，并最终选择更稳妥的交付策略。

修改前后：

```diff
- 只交付 CLI，不包含 UI
+ 本地 CLI 提供全部控制能力
+ 静态 WebUI 只展示脱敏报告
+ 公网页面只包含 scripted mock 数据
+ 网页不能执行、审批、访问文件、SQLite 或 Keyring
```

### 迭代 3：把治理从提示词变成代码机制

治理体验比较了“每次写入都审批、三级风险、永不直接写文件”。负责人选择三级风险：

> “信任度较高、风险较低的可以适当放给agent自主解决，同时能够体现治理。”

随后治理通过多个独立决策深化：

- 当场询问同时持久化待审批状态。
- 审批绑定动作指纹、会话、有效期，并只能消费一次。
- 项目配置无法关闭硬性安全底线。
- 命令使用结构化 `shell=False` 子进程和最小白名单。
- 达到预算时暂停，而不是宣称成功。
- 文件变更有日志，失败时由用户选择保留或回滚。

负责人特别强调：

> “达到任一上限时不宣称失败修复成功，而是暂停并让用户查看当前状态、继续、保留或回滚很重要，也很能体现用户优先。”

这让治理从简单命令黑名单变成完整、持久化、可单测的状态系统。

### 迭代 4：防止记忆和配置成为绕过通道

记忆方案比较了“仅手工备注、自动保存完整历史、受治理结构化条目”。负责人选择第三种。模型只能提出候选，必须通过验证证据或用户批准才能进入可信上下文。

配置方案选择分层信任：

> “可信用户策略+项目配置分层，不同信任度任务的配置信任边界应当分层处理。”

因此形成关键不变量：低信任配置只能收紧，不能扩大权限；被修复项目不能给自己的危险命令加白。

修改前后：

```diff
- 记忆：未定义的历史文本或备注
+ 记忆：candidate -> approved/verified -> active
- 配置：单一项目策略文件
+ 配置：内置底线 -> 用户策略 -> 项目限制 -> 会话限制
```

### 迭代 5：选择整体架构

Visual companion 展示三种完整架构：

1. 中央策略网关循环。
2. 批量补丁规划器。
3. 事件驱动插件平台。

负责人选择第一种，并给出原因：

> “B的反馈粒度粗，C复杂度相对太高了。”

中央策略网关保证 Parser 和 Policy 位于所有副作用之前，同时保留逐动作反馈；它没有引入四天内无法验证的通用事件/插件平台。

随后又展示并批准了完整组件图和治理状态机。产品范围、架构、治理、数据隐私、主循环错误处理、测试交付均逐节获得明确批准，之后才开始写 `SPEC.md`。

## 6. AI 建议中被采纳的部分

| 建议 | 采纳原因 |
|---|---|
| 把失败测试修复作为主场景 | 客观、可复现，适合紧凑的端到端演示 |
| Python-first 而非多语言插件 | 符合用户选择和时间限制，同时保留验证器扩展点 |
| 本地工作区而非 Git clone/push | 使用常见，边界清晰，不引入远程凭据 |
| 三级风险 | 在自主性和安全之间取得平衡 |
| 中央策略网关 | 所有副作用前只有一个不可绕过、可审计入口 |
| 持久化 HITL 与防重放 | 治理深度超过简单 `y/N` |
| 精确替换而不是 shell 修改 | 确定性错误、易测试、可原子写入 |
| 结构化 `shell=False` 子进程 | 能运行验证器，又不开放原始 shell |
| SQLite + 备份 + JSONL | 无外部服务即可事务化、恢复、回滚和审计 |
| OS Keyring | 用较小抽象满足凭据要求 |
| 受治理结构化记忆 | 防止模型结论静默进入可信上下文 |
| 静态报告页 | 提供 UI 证据，同时保持无控制面的安全边界 |
| Open Design 组合 | 给信息密集的开发者报告提供一致设计，而不扩大前端范围 |

## 7. 被负责人推翻或修正的 AI 建议

| 初始 AI 方向 | 人工修正 | 原因与影响 |
|---|---|---|
| 早期准备建议 TypeScript/Node | 使用 Python | 负责人重视 Python 开发效率，且 Conda 已配置 |
| 宽泛的通用 GuardedLoop | 聚焦失败 Python 测试 | 产品变得客观可验收且可在时限内交付 |
| 曾考虑更完整或实时 WebUI | CLI 主控，网页静态只读 | 显著减少攻击面和前端工作 |
| AI 一度把 WebUI 当作无歧义硬要求 | 负责人指出文档内部矛盾 | 迫使 AI 回到证据，而不是机械坚持 |
| 纠正后短暂接受纯 CLI | 负责人进一步核对后恢复最小查看器 | 降低评分风险，同时保持范围 |
| 早期考虑 Docker 默认分发 | Python 包 + 静态 Pages | 更适合主机 OS Keyring 和 CLI 产品 |
| 批量规划或事件插件架构 | 中央策略循环 | 反馈更细，实施风险更低 |

这些修正证明负责人承担了 PM 和最终 reviewer 责任，而不是直接采用 AI 的第一版建议。

## 8. Visual Companion 过程证据

负责人同意使用 Superpowers visual companion。所有 companion 文件位于 Codex 独立可视化目录，没有写入 Git 仓库。

展示内容：

1. 三种整体架构及其取舍。
2. 中央策略网关的组件、数据流和信任边界。
3. 风险分类、审批生命周期和持久化会话状态机。

浏览器事件记录了中央策略架构选择和架构批准；终端消息是所有设计批准的最终权威证据。治理图也在终端得到明确批准。

工具过程暴露了两个限制：

- Windows Git Bash 需要显式 login shell 才能得到完整 PATH。
- 本地 companion 服务会在长时间讨论后因 idle timeout 停止。

这些问题没有影响仓库，但使视觉流程不如纯文本连贯。它们被保留为真实过程证据，而不是隐藏。

## 9. 对 Brainstorming Skill 的反思

### 做得好的地方

- 一次一个问题，使没有相关经验的负责人能够理解复杂项目。
- 强制比较方案，避免第一想法直接成为设计。
- 分节批准使范围、架构、治理、数据、循环和交付可以独立审查。
- 暴露了容易忽略的问题：审批重放、工作区漂移、记忆污染、配置越权、输出脱敏和回滚日志。
- 硬性设计门禁阻止了在要求仍矛盾时提前编码。
- 可视化比较清楚展示中央策略、批量规划和事件架构的区别。

### 不满意的地方

- 每题三选一和逐节确认在四天期限下消耗了较多时间。
- AI 起初过度强调 WebUI，必须由负责人纠正。
- 某些低风险技术细节可以在主要风险模型确定后合并决定，不必每项单独占一轮。
- Visual companion 在 Windows 的启动和超时问题中断了节奏。
- Brainstorming 只能提高设计清晰度，不能单独证明 PLAN 可实施；真正的异构智能体实现试运行仍比同类型只读审查提供更强证据。

### 批判性认识

Superpowers 假设：先把歧义显式化，能减少实现返工。这个假设在本项目基本成立，因为治理、凭据和 UI 边界都在讨论中发生了实质变化。

但如果把每个可逆小细节都当作架构决定逐项签字，流程会变成形式负担。后续 PLAN 和实现应保留 TDD、评审和验证纪律，同时对低风险实现细节采用保守默认值。

## 10. 当前交付物状态

- `SPEC.md`：已根据全部批准设计写入中文，并于 2026-08-11 通过负责人审阅。
- `SPEC_PROCESS.md`：已记录 brainstorming、计划与 fresh Codex 替代冷读审查；实现期间继续更新。
- `PLAN.md`：已获负责人批准并按四个 PR 波次执行；Task 1-15、远端 CI/Pages 与课程 ZIP 合同均已关闭。
- `AGENT_LOG.md`：已记录到 PR-04 合并后验收；PR、main CI、artifact、Pages 与外部 ZIP 证据均来自真实操作。
- `REFLECTION.md`：负责人主工作区存在 staged 用户版本；PR04 未读取、未修改、未取消暂存、未提交。
- 实现源码：Task 1-14 已完成；2026-08-14 本地 `verify.ps1`、治理演示、安全回归、扫描和 wheel 安装验证均通过。

### Task 15 本地交付验证

Task 15 是验证任务，按计划豁免人为制造 RED。新鲜 `scripts/verify.ps1` 在 Python 3.13.11
下得到 `352 passed, 3 skipped`；3 个 skip 均为 Windows `WinError 1314` 符号链接权限，
Ruff、strict mypy（23 个源码文件）和 no-isolation build 全部通过。指定治理回归为
`139 passed`，直接 CLI demo 和安装 wheel 后的 demo 均为三幕 PASS，且输出明确表明未使用网络或
真实 Keyring。

最初把 pytest basetemp 放在仓库根下时，Ruff 真实扫描到测试夹具故意生成的非法
`pyproject.toml`；改为不存在的嵌套 `.pytest_cache/task15-verify` 又触发 pytest
`FileNotFoundError`。根因确认后仅调整进程级 `PYTEST_ADDOPTS` 为 Ruff 默认排除且 pytest 可直接
创建的 `.pytest_cache`，完整门禁随即通过；没有为环境问题修改生产代码或验证脚本。

修正后的大小写敏感 tracked-text 凭据扫描恰好命中 `PLAN.md:2283` 的一个公开假 fixture：
`provider.invalid`，且 `api_key` 字段的值为 `test-secret`；真实或未解释凭据为 0。计划内/扩展 tracked
敏感产物路径扫描为 0 命中。wheel 与 sdist 各一个并包含四个静态 WebUI 资源；本地 wheel 在 `PIP_NO_INDEX=1` 的唯一 TEMP venv 中以 `--no-deps`
安装，导入路径确认位于该 venv 的 `site-packages`，验证后临时目录已安全删除。远端 GitHub
Actions、artifact、Pages URL、PR 和课程 ZIP 尚未在本阶段验证，因此没有写入推测结果。

独立规格审查和质量审查最初均返回 `CHANGES REQUIRED`：凭据扫描命令存在 PowerShell 假阴性，
sdist 精确散列是会被后续文档修改改变的自引用快照，SPEC 状态也过早写成全部实现完成。本轮已
修正三项表述与证据。质量复审已经 `APPROVED`；规格复审又发现 PLAN Step 3 仍把不可靠的
`git grep` 作为 Run/Expected 合同，现已明确 supersede 旧命令并加入只读取 tracked 文本后缀、
使用大小写敏感 `Select-String` 且自带假 fixture 分类断言的可复制 PowerShell scanner。该修复
完成时保持等待规格再次复审；最终规格复审现已 `APPROVED`，质量复审同样为 `APPROVED`；两项最终复审均
确认没有遗留 finding。该段是 Task 15 本地阶段的历史快照；后续 PR-04 合并后证据记录如下。

### PR-04 合并后交付验证

PR #4 于 2026-08-14 合并到 `main`，merge commit 为
`9bab6be9db3556dd7f2f4e542ef9a5ec82a0acb0`。该 commit 的 GitHub Actions CI run
`31780633358` 与 Static WebUI run `31780633378` 均为 `success`；前者完成 pytest、Ruff、
strict mypy、build 与 distribution artifact，后者完成静态报告测试、仅复制 WebUI 资源并部署
Pages。公开 URL `https://jiezhouziheng.github.io/Coding_Agent_Harness_Jie/` 已在桌面与移动视口
实际打开，只有静态 mock 报告，无表单、按钮、输入、控制 API、SQLite、WebSocket 或审批能力。

课程 ZIP 使用 `git archive` 从最终已跟踪树生成；必需源码、测试、规格、过程文档、CI、脚本和
静态 WebUI 均在，Git 元数据、凭据、本地状态、审计、备份、私有报告、缓存和构建产物均不在。
精确 post-commit 文件名、大小与 SHA-256 记录在 PR #4 外部证据中，避免包含本段的归档对自身
散列形成循环依赖。

### PLAN 生成与自审

课程要求根目录 `PLAN.md`，因此覆盖 Skill 默认的 `docs/superpowers/plans/` 路径。计划按依赖波次拆成 15 个任务，为每个实现任务写明精确文件、失败测试、红灯命令、实现契约、绿灯命令、两阶段评审和独立提交；Task 15 是只执行验证与交付的最终任务，不伪造红灯步骤。

计划自审覆盖 18 条 SPEC 验收标准和六个 Harness 维度。自审过程中发现并修正了四类会影响冷启动的歧义：测试 fixture 没有任务归属、暂停状态没有全部纳入单写会话索引、`PolicyGateway` 未明确承担“规范化后再记录”的职责、CLI 示例只有签名而没有应用服务调用。还修正了 `lstrip("./")` 可能错误移除 `.env` 前导点的安全问题。当前检查结果为 15/15 个任务契约完整、18/18 条 AC 有映射、6/6 个维度有实现证据、无待填占位标记，代码围栏成对。

## 11. 替代冷启动审查

原定执行协议如下：

1. 使用与主 Codex App 不同类型的 Claude Code。
2. 启动全新 session，不导入对话、任务 memory 或口头解释。
3. 只提供已批准的 `SPEC.md` 和 `PLAN.md`。
4. 使用一次性隔离 worktree。
5. 要求它选择并尝试 1-2 个 PLAN task，时间约 1-2 小时。
6. 指示它遇到未写明的决定时暂停提问，不得猜测。
7. 试验期间不补充隐藏上下文。
8. 记录每个澄清问题、非预期解读、实现阻塞、测试结果和产出差异。
9. 判断每个问题属于 SPEC 缺陷、PLAN 缺陷还是 agent 阅读错误。
10. 在本文件加入全部 `SPEC.md`/`PLAN.md` 修订前后 diff。
11. 试验实现必须丢弃或重新独立评审，不能静默视为正式产物。

### 实际审查方法

项目在截止期时间盒与资源约束下没有启动外部 Claude Code 实现 session，改用一个 `fork_turns="none"` 的 fresh Codex 子智能体进行受限上下文审查。该智能体未继承当前对话和 memory，只被允许读取 `SPEC.md` 的相关章节与 `PLAN.md` 至 Task 2 结束；禁止修改文件、运行测试或继续阅读后续任务。

第一次广范围审查没有在时间盒内收敛，被中断且不作为成功证据。第二次将范围严格限制为 Task 1-2 后完成只读报告。它不是 Claude Code，也没有实现 task、测试输出或实验 commit，不能声称完全满足课程 §4.5 的异构智能体实现试运行。

### 审查发现与处理

| 级别 | fresh Codex 发现 | 分类 | 修订前 | 修订后 |
|---|---|---|---|---|
| Critical | Task 1 的安装元数据依赖 README，但计划未显式确认文件存在 | PLAN 缺陷 | 直接生成 `pyproject.toml` 并安装 | 增加 README 存在性 preflight；README 已存在且 Task 1 不修改 |
| Critical | 七个空 Typer 子应用不保证稳定出现在 help 中 | PLAN 缺陷 | 只创建并挂载空 group | 为每个 group 注册 callback，保持命令组可见 |
| Important | 缺少依赖时，首个测试会因环境而不是缺实现失败 | PLAN 缺陷 | 直接运行红灯测试 | 先验证 Python、Typer、pytest；缺依赖时暂停申请安装 |
| Important | 严格模型仍可能接受类型强制转换 | PLAN/SPEC 落地缺陷 | `ConfigDict(extra="forbid", frozen=True)` | 增加 `strict=True` 和错误类型测试 |
| Important | Task 2 声称覆盖全部 Action/Decision/ValidationResult，但测试样例不足 | PLAN 缺陷 | 只测 replace 和少量非法输入 | 参数化覆盖 8 种 Action，并补 Decision、ValidationResult 测试 |
| Important | 稳定接口用 `Literal`，实现改用 `StrEnum` | PLAN 内部矛盾 | 接口表与实现不一致 | 稳定接口表统一为 `StrEnum` |
| Minor | `ReadFileAction` 未验证 `end_line >= start_line` | PLAN 缺陷 | 只有单字段下限 | 增加跨字段 validator 和失败测试 |

审查结论为：原 Task 1-2 在修订前不适合无历史智能体直接执行；上述修订完成后，未再发现阻塞正式 Task 1 的文档问题。没有修改产品范围或安全边界，因此无需改变用户故事和 AC。

### 残余风险与补偿

替代审查证明了受限上下文下的静态可读性，但没有证明异构工具链中的实际实现行为。补偿措施为：正式实现使用隔离 worktree；每个 Task 使用 fresh subagent；严格执行测试先行的红-绿循环；每个 Task 先做 spec 合规评审，再做代码质量评审；所有真实问题、人工修改和 commit 继续写入 `AGENT_LOG.md`。

相关阻塞已经修订，`SPEC.md` 实现门禁打开，产品行为合同和验收标准不变。

## 12. 工作流偏离与明确决定

- Superpowers 默认设计文档路径被课程要求的根目录 `SPEC.md` 覆盖。
- GitHub 是仓库和 CI 主平台；最终通过课程网站提交源码/文档 ZIP，不使用 NJU Git。
- GitHub Actions 是实际执行 CI；保留最小 `.gitlab-ci.yml` 作为课程字面要求文件。
- Open Design 仅用于静态报告页，不属于运行时 Harness 机制。
- Brainstorming 期间没有生成代码、项目骨架、安装依赖或编写实现计划。
- 课程要求的异构智能体实现试运行在时间盒与资源约束下调整为 fresh Codex 受限上下文只读审查；文档明确两者不等价，并记录实际发现与修订。
