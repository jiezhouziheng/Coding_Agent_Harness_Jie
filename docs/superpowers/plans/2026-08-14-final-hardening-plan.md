# PR-05 Final Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭审批绑定、终态恢复和工作区互斥的集成缺口，并形成可直接打包提交的最终远端证据。

**Architecture:** 保留 PolicyGateway、Dispatcher、SessionService 和 HarnessApplication 现有职责。Dispatcher 负责执行前证据闭合，SessionService 负责恢复状态与恢复锁，HarnessApplication 负责新会话锁；不引入新框架或新持久化模型。

**Tech Stack:** Python 3.13、Pydantic、SQLite、pytest、Ruff、mypy、GitHub Actions。

---

### Task 1: 固化设计与分支基线

**Files:**
- Create: `docs/superpowers/specs/2026-08-14-final-hardening-design.md`
- Create: `docs/superpowers/plans/2026-08-14-final-hardening-plan.md`

- [x] **Step 1: 从本地最终提交创建 `feature/pr05-final-hardening` worktree**
- [x] **Step 2: 以本 worktree `src` 运行全量基线**

Run: `python -m pytest -q`
Expected: `371 passed, 3 skipped`，skip 仅为 Windows symlink privilege。

- [ ] **Step 3: 提交设计与计划**

Commit: `docs: design PR-05 final hardening [agent: codex-main]`

### Task 2: Dispatcher 完整验证授权证据

**Files:**
- Modify: `tests/test_dispatcher.py`
- Modify: `src/coding_agent_harness/dispatcher.py`

- [ ] **Step 1: 写入真实 StateStore 跨会话审批复用回归测试**

测试建立两个包含同一规范化动作的会话，只消费会话 A 的审批，再把 A 的 approval id 放入会话 B grant；断言 `Dispatcher.execute` 抛出 `DispatchError` 且 B 的目标文件/变更日志保持不变。

- [ ] **Step 2: 运行定向测试并确认旧实现真实失败**

Run: `python -m pytest tests/test_dispatcher.py -q`
Expected: forged grant 被执行，新增测试 FAIL。

- [ ] **Step 3: 最小实现完整绑定**

```python
decision = self.store.get_policy_decision(grant.policy_decision_id)
stored_action = self.store.get_action(grant.action_id)
if decision.action_id != grant.action_id:
    raise DispatchError("grant_decision_mismatch")
if (
    stored_action.session_id != grant.session_id
    or stored_action.fingerprint != grant.fingerprint
    or stored_action.action != grant.action
):
    raise DispatchError("grant_action_mismatch")
```

审批查询必须传入 `action_id`、`session_id`、`policy_decision_id`。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

### Task 3: 恢复状态 fail closed

**Files:**
- Modify: `tests/test_recovery.py`
- Modify: `tests/test_engine.py`
- Modify: `src/coding_agent_harness/session_service.py`
- Modify: `src/coding_agent_harness/engine.py`

- [ ] **Step 1: 写入终态和用户决策状态零副作用测试**

对 `SUCCEEDED`、`NEEDS_USER_DECISION`、`CHANGES_KEPT`、`ROLLED_BACK` 调用 resume；断言 engine factory 未创建、文件与 change journal 不变、返回状态不变。直接调用 Engine 的对应测试应得到稳定 `ValueError("session_not_runnable")`。

- [ ] **Step 2: 运行测试并确认旧实现真实 RED**
- [ ] **Step 3: 在 SessionService 和 Engine 增加显式可运行状态集合**

```python
RUNNABLE_SESSION_STATUSES = {
    SessionStatus.CREATED,
    SessionStatus.RUNNING,
    SessionStatus.PAUSED_APPROVAL,
}
```

SessionService 对集合外状态原样返回；Engine 对集合外状态抛稳定错误。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

### Task 4: 将工作区锁接入运行与恢复入口

**Files:**
- Modify: `tests/test_application.py`
- Modify: `tests/test_integration.py`
- Modify: `src/coding_agent_harness/application.py`
- Modify: `src/coding_agent_harness/session_service.py`

- [ ] **Step 1: 写入入口锁竞争与异常释放测试**

测试预先占用同工作区锁后 `app.run` / `resume_and_run` 均抛 `WorkspaceBusy` 且不执行 LLM/工具；另让 Engine 抛错并确认锁可再次获取。

- [ ] **Step 2: 运行测试并确认旧实现真实 RED**
- [ ] **Step 3: 用 `try/finally` 包住完整入口生命周期**

```python
lock = self.sessions.acquire_workspace(selected_workspace)
try:
    session_id, engine = self.engine_factory.create(...)
    return engine.run(session_id)
finally:
    lock.release()
```

恢复入口先从 Project 读取规范路径，获取相同 lock_root 下的锁，再进入现有恢复逻辑。

- [ ] **Step 4: 运行定向与集成测试并确认 GREEN**

### Task 5: 文档、验证、PR 与最终归档

**Files:**
- Modify: `README.md`
- Modify: `REFLECTION.md`
- Modify: `PLAN.md`
- Modify: `SPEC_PROCESS.md`
- Modify: `AGENT_LOG.md`

- [ ] **Step 1: 更新限制、修复证据、AI 辅助声明和 PR-05 记录**
- [ ] **Step 2: 运行全量 pytest、Ruff、strict mypy、build、diff 和凭据扫描**
- [ ] **Step 3: 提交实现与文档，推送分支并创建 PR #5**
- [ ] **Step 4: 等待 GitHub CI 通过，合并 PR #5**
- [ ] **Step 5: 从最终 `origin/main` 生成课程 ZIP，核验清单和 SHA-256**

