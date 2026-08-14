# PR-05 最终治理加固设计

## 背景与目标

最终只读审查确认，既有 371 项测试虽全部通过，但运行时仍有三个纵向集成缺口：Dispatcher 没有消费存储层已经支持的完整审批绑定；终态会话可再次进入 Engine 并在报错前产生副作用；工作区锁只有独立单元测试，没有进入真实 `run` / `resume` 调用链。

本轮目标是不扩展产品功能，只关闭这三个可复现缺陷，并补齐反思、过程记录、远端 CI 与最终 ZIP 证据，使公开仓库成为课程实际提交版本。

## 方案比较

1. **最小加固 PR（采用）**：保留现有组件边界，在 Dispatcher、SessionService 和 HarnessApplication 的入口补齐校验与锁；用真实存储/应用集成测试证明行为。风险和改动面最小。
2. **仅补当前症状**：只给 `is_consumed_approval` 增加三个参数。速度最快，但 ALLOW grant 的 Action/Decision 关系和运行入口并发问题仍未闭合。
3. **重构授权网关**：把 grant 验证、锁和恢复状态机重新抽成新服务。结构可能更统一，但截止前回归风险过高，也不符合 YAGNI。

## 设计

### 1. 授权证据完整绑定

Dispatcher 在执行任何动作前必须：

- 验证动作规范化指纹等于 grant 指纹；
- 读取持久化 PolicyDecision，确认其 `action_id` 等于 grant 的 `action_id`；
- 读取持久化 Action，确认会话、指纹和规范化动作均与 grant 一致；
- 对 `REQUIRE_APPROVAL` 再以 `approval_id`、fingerprint、action_id、session_id、policy_decision_id 查询已消费审批；
- 任一不一致都以稳定 `DispatchError` fail closed，且工具不得被调用。

### 2. 恢复状态门禁

`resume_and_run` 只允许以下状态进入执行路径：

- `CREATED`：启动尚未运行的会话；
- `RUNNING`：恢复中断的循环；
- `PAUSED_APPROVAL`：按审批状态消费、反馈或继续。

`PAUSED_WORKSPACE_DRIFT`、`PAUSED_LIMIT_REACHED`、`PAUSED_PROTOCOL_ERROR`、`PAUSED_INTERNAL_ERROR`、`SUCCEEDED`、`NEEDS_USER_DECISION`、`CHANGES_KEPT`、`ROLLED_BACK` 均原样返回。它们需要用户显式解决漂移、预算或变更处置，不能通过普通 resume 隐式越过。Engine 自身也增加相同的防御性入口检查，避免绕过 SessionService 直接调用。

### 3. 工作区互斥

`HarnessApplication.run` 在创建会话前获取规范化工作区对应的锁，并在成功、暂停或异常时统一释放。`SessionService.resume_and_run` 从持久化 Project 得到工作区后获取同一把锁，再执行恢复检查和 Engine。锁文件继续位于应用数据目录，不写入用户项目。

### 4. 错误处理与测试

- 锁竞争继续使用稳定 `WorkspaceBusy("workspace_busy")`；
- 状态门禁返回持久化 session，不制造非法转换；
- 审批绑定不匹配使用稳定 DispatchError，不泄漏路径或凭据；
- 回归测试必须先在旧实现上得到真实 RED，再写最小生产修复；
- 最后运行全量 pytest、Ruff、strict mypy、wheel/sdist、diff/凭据扫描。

## 交付与学术边界

`REFLECTION.md` 继续明确标注 Codex 辅助整理。负责人需在提交课程网站前阅读并确认其中观点符合本人经历；仓库和 PR 不把它描述为未使用 AI。PR 描述与 commit message 记录 agent 和人工修改情况。合并后从最终远端 main 用 `git archive` 重新生成 ZIP，确保包含反思、源码、测试和全部过程文档。

