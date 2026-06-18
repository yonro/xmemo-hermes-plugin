# XMemo x Hermes Agent 记忆体插件执行后深度审核与提升计划

> 状态：执行后审核修订版  
> 审核日期：2026-06-18  
> 审核者：pc-codex  
> 目标：让 XMemo 记忆体工具真正符合 Hermes Agent 的插件边界、缓存约束、profile 隔离、工具面成本和 XMemo REST 合约。

---

## 0. 最终判断

### 0.1 结论

当前实现已经证明：**XMemo 作为 Hermes `MemoryProvider` 的技术路线可行**。

但它还不能被称为“完美匹配 Hermes”或“符合 Hermes 官方插件发布要求”。主要原因不是 `MemoryProvider` ABC 本身，而是以下执行后偏差：

- Hermes 根规则已经明确：**新 memory provider 不应再加入 `plugins/memory/` 树内**，应作为外部插件安装到 `$HERMES_HOME/plugins/<name>/` 或经官方确认的独立分发方式。
- 当前 XMemo provider 放在 `plugins/memory/xmemo/`，并且内部使用 `from plugins.memory.xmemo...` 绝对导入；这在树内可用，但对用户安装的外部插件形态不兼容。
- 当前默认暴露 10 个工具，包含 destructive `xmemo_forget` 和多个高级 workflow 工具，工具面偏大，不符合 Hermes “核心窄腰 / 工具 schema 成本高”的风格。
- `sync_turn()` 现在每轮都写 timeline event，和原计划“只写高信号事实/状态”的原则冲突，会制造低信号噪声并可能记录敏感用户内容。
- prefetch cache 不是按 `session_id` 分桶，gateway 多会话下存在召回串扰风险。
- XMemo REST client 有两个路径与 Memory OS 官方 client/API 不一致：`mark_used` 和 `forget`。
- 配置层仍允许从 `xmemo.json` 读取 `api_key`，与“secret 只来自 env/secret store，不写普通 JSON”的要求冲突。
- 本地测试没有跑通：按 Hermes 要求执行 `bash scripts/run_tests.sh ...` 时，当前 checkout 缺少 `.venv`/`venv`，测试 wrapper 直接退出。

### 0.2 Go / No-Go

**No-Go：当前实现不应作为上游 Hermes 官方 PR 直接提交。**

**可以作为内部 spike / fork 验证，但发布或上游前必须完成 P0 修复。**

P0 完成后才可以进入：

- 外部插件包发布。
- Hermes 兼容性认证。
- XMemo 官方 “Hermes memory provider” 文档。
- live E2E。

---

## 1. 已核对的 Hermes 事实

### 1.1 官方仓库政策

来自 `AGENTS.md` 的关键约束：

- Per-conversation prompt caching 是核心约束；不要在会话中途改变系统 prompt、工具集或历史上下文。
- 每个 model tool 都会进入 API call，新增工具面的成本很高。
- 新 capability 应优先走 CLI/skill/service-gated tool/plugin/MCP，核心 tool 是最后选择。
- Memory provider 插件实现 `agent/memory_provider.py::MemoryProvider`。
- Memory provider 生命周期由 `agent/memory_manager.py::MemoryManager` 编排。
- `is_available()` 必须便宜，不能做网络调用。
- 新 memory backend 不再进入 `plugins/memory/` 树内；应作为外部插件发布。
- 插件不应修改核心文件；需要能力时扩展通用插件 surface，不能写 XMemo 专用核心逻辑。

### 1.2 MemoryProvider 合约

`agent/memory_provider.py` 的实际接口包括：

- `name` 是 abstract property。
- `is_available()` 不做网络调用。
- `initialize(session_id, **kwargs)` 会收到 `hermes_home`、`platform`、`agent_context`、`agent_identity`、gateway/user/chat/thread 等上下文。
- `system_prompt_block()` 只放静态 provider 说明。
- `prefetch(query, session_id=...)` 返回 raw context；Hermes 统一调用 `build_memory_context_block()` 包 `<memory-context>`。
- `queue_prefetch(query, session_id=...)` 用于下一轮 prefetch。
- `sync_turn(user_content, assistant_content, session_id=..., messages=...)` 应非阻塞。
- `get_tool_schemas()` 暴露 provider tools。
- `handle_tool_call()` 必须返回 JSON string。
- 可选 hooks：`on_turn_start`、`on_session_end`、`on_session_switch`、`on_pre_compress`、`on_memory_write`、`on_delegation`。

### 1.3 Hermes 运行时注入

当前 Hermes 的真实链路：

- `agent/agent_init.py` 从 `config.yaml` 的 `memory.provider` 读取 provider 名称。
- `plugins/memory/__init__.py` 加载 bundled provider 或 `$HERMES_HOME/plugins/<name>/` user provider。
- `_mp.is_available()` 返回 true 后，`MemoryManager.add_provider()` 注册。
- `MemoryManager.initialize_all()` 注入 `hermes_home`、`platform`、`agent_context`、`agent_identity`、gateway identity 等。
- `agent/turn_context.py` 每轮调用 `MemoryManager.prefetch_all()`。
- `agent/conversation_loop.py` 只在当前 API call 的 user message 上注入 memory context，不改写持久 `messages`。
- `agent/turn_finalizer.py` 结束 turn 后调用 `_sync_external_memory_for_turn()`，再由 `MemoryManager.sync_all()` 和 `queue_prefetch_all()` 后台处理。
- 内置 `memory` tool 的 `add` / `replace` 会桥接到 `MemoryManager.on_memory_write()`。

结论：XMemo 不需要改 Hermes 核心就能做到高质量集成；关键是 provider 自身要遵守这些契约。

---

## 2. 当前实现审核

### 2.1 已完成且方向正确

当前 `plugins/memory/xmemo/` 已经实现：

- `XMemoMemoryProvider` 继承 `MemoryProvider`。
- `name` 是 property，返回 `"xmemo"`。
- `is_available()` 基本只检查配置，不做网络调用。
- `get_config_schema()` 和 `save_config()` 已接入 setup wizard。
- `post_setup()` 可以让 `hermes memory setup xmemo` 走 provider 自己的 setup。
- 使用 `httpx.Client` 轻量同步 REST client，没有引入 `memory-os` server dependency tree。
- `prefetch()` 返回 raw-ish context，由 Hermes 统一 fencing。
- `client.py` 覆盖基础 REST：health、recall_context、search、remember、update_state、timeline、reminders、snapshot。
- 测试文件 `tests/plugins/memory/test_xmemo.py` 覆盖了 provider 工具路由、setup、prefetch、basic circuit breaker、secret 不写入 save_config。

这些点说明原架构方向成立。

### 2.2 P0 问题：官方插件发布形态不符合

当前代码放在：

```text
plugins/memory/xmemo/
```

但 Hermes 仓库规则明确：新 memory backend 不应进入树内，应作为外部插件发布。

更关键的是，当前实现内部多处使用 bundled 路径：

```python
from plugins.memory.xmemo.config import load_config
from plugins.memory.xmemo.client import XMemoClient
from plugins.memory.xmemo.cli import cmd_setup
```

这会导致：

- 在树内运行时可用。
- 安装到 `$HERMES_HOME/plugins/xmemo/` 时，模块名会是 `_hermes_user_memory.xmemo`，绝对导入会失败。
- 如果树内和用户外部插件同名并存，可能误导入 bundled 版本。

**修正要求：**

- 把 XMemo provider 抽成独立插件仓库，例如 `xmemo-hermes-plugin`。
- 插件代码内部全部改为相对导入：

```python
from .config import load_config
from .client import XMemoClient
from .cli import cmd_setup
```

- 测试必须覆盖 user-installed plugin path：把插件复制到 temp `$HERMES_HOME/plugins/xmemo/` 后通过 `load_memory_provider("xmemo")` 加载。
- 如果要支持 pip 分发，先确认 Hermes 当前 memory discovery 是否真的支持 memory-provider entry points；当前已核对代码主要支持 bundled 和 `$HERMES_HOME/plugins/<name>/` 目录。

### 2.3 P0 问题：REST endpoint 偏差

当前 `plugins/memory/xmemo/client.py`：

```python
POST /v1/memories/{memory_id}/used
POST /v1/forget/{target}
```

Memory OS 官方 client/API 显示：

```text
POST /v1/memories/{memory_id}/usage
POST /v1/memories/{memory_id}/forget
```

**修正要求：**

- `mark_used()` 改为 `/v1/memories/{memory_id}/usage`。
- payload 支持 `usage_tracking_id`、`action`、`context`、`metadata`。
- `forget()` 改为 `/v1/memories/{memory_id}/forget`。
- 禁止 `target="current"` 这种不稳定语义进入默认工具；必须要求明确 memory id。
- 用 `httpx.MockTransport` 增加 endpoint path 测试，不能只用 fake client method 覆盖。

### 2.4 P0 问题：每轮自动写 timeline 噪声过大

当前 `sync_turn()` 每轮都会：

```python
summary = f"Turn {self._turn_count}: user asked about {user_content[:120]}..."
client.record_event(...)
```

这违反原计划 D4：

- MVP 不应把每个 turn 全量或摘要写成长期记忆。
- 普通对话 turn 不应写入 semantic/timeline memory。
- 用户内容前 120 字可能包含 secret、路径、错误日志、个人信息或低信号闲聊。

**修正要求：**

- 默认关闭 every-turn timeline write。
- `sync_turn()` 只做高信号捕获：
  - 用户明确说“记住 / 保存 / 以后记得”。
  - 架构决策、修复根因、用户纠正、长期偏好、可复用 runbook。
  - 明确 handoff / blocked 状态。
- 或者 Phase 1 完全禁用自动写入，只保留显式 `xmemo_remember`、`xmemo_update_state`、`on_memory_write()` mirror。
- 若需要事件流，放到配置项 `memory.xmemo.capture_timeline: true`，默认 false。
- 写入前做长度上限、secret redaction、metadata provenance。

### 2.5 P0 问题：prefetch cache 未按 session 隔离

当前 provider 只有：

```python
self._prefetch_result = ""
self._prefetch_thread = None
```

`queue_prefetch(query, session_id=...)` 接收 `session_id`，但存储时没有按 session 分桶。

Hermes gateway 会服务多个 user/chat/session；这种全局单槽 cache 可能导致：

- A 会话的 recall 被 B 会话消费。
- 用户之间记忆上下文串扰。
- 多平台 gateway 下出现难以复现的隐私 bug。

**修正要求：**

- 改成：

```python
self._prefetch_results: dict[str, str]
self._prefetch_threads: dict[str, threading.Thread]
```

- key 使用 `session_id or self._session_id or "__default__"`。
- `prefetch()` 只消费同 session 的结果。
- `on_session_switch(reset=True)` 清理旧 session cache。
- 增加 gateway 并发测试：两个 session 排队不同 query，只能消费自己的 recall。

### 2.6 P0 问题：secret 仍可能从 JSON 读取

`config.py` 注释说 secret 只从 env 读取，但代码仍有：

```python
api_key = (
    os.environ.get("XMEMO_KEY")
    or os.environ.get("MEMORY_OS_API_KEY")
    or file_cfg.get("api_key", "")
)
```

这意味着历史或手工写入的 `$HERMES_HOME/xmemo.json` 中 `api_key` 仍会被使用。

**修正要求：**

- 删除 `file_cfg.get("api_key", "")` fallback。
- 如果发现 JSON 中存在 `api_key`，只记录 redacted warning，并在下次 `save_config()` 时移除。
- 测试覆盖：JSON 里有 `api_key`，但 env 无 key，则 `is_available()` 必须 false。

### 2.7 P0 问题：`is_available()` 有文件写入副作用

`is_available()` 调用 `load_config()`，而 `load_config()` 会在缺少 `agent_instance_id` 时调用 `save_config(config)`。

Hermes 会在 discovery/status 路径调用 `is_available()`；这个路径应该便宜、无网络、无副作用。

**修正要求：**

- 拆分：

```python
load_config(create_instance: bool = False)
ensure_agent_instance_id(config, hermes_home)
```

- `is_available()` 使用 `load_config(create_instance=False)`。
- `initialize()` 或 setup 才生成并持久化 instance id。

### 2.8 P0 问题：默认工具面过大

当前默认暴露 10 个工具：

```text
xmemo_search
xmemo_remember
xmemo_update_state
xmemo_recall_context
xmemo_record_event
xmemo_create_reminder
xmemo_list_reminders
xmemo_complete_reminder
xmemo_mark_used
xmemo_forget
```

Hermes 明确强调 model tool schema 成本。这个默认面偏大，尤其：

- `xmemo_forget` 是 destructive tool，缺少确认语义。
- `xmemo_mark_used` 应该更多是 provider 内部自动反馈，不一定需要暴露给模型。
- reminders/decisions/snapshot 是 workflow 能力，适合二阶段或配置开启。

**修正要求：**

默认只暴露 MVP 工具：

```text
xmemo_recall_context
xmemo_search
xmemo_remember
xmemo_update_state
```

可选工具通过 config gate：

```yaml
memory:
  xmemo:
    enable_workflow_tools: true
    enable_destructive_tools: false
```

destructive `xmemo_forget` 默认不暴露；若开启，必须要求 exact memory id，不支持 `"current"`。

### 2.9 P1 问题：未实现 `on_memory_write()` mirror

Hermes 已经在内置 `memory` tool 的 `add` / `replace` 后调用：

```python
MemoryManager.on_memory_write(...)
```

当前 XMemo provider 没有 override `on_memory_write()`。

结果：

- 用户或模型调用 Hermes 内置 memory tool 时，XMemo 不会同步。
- XMemo 和 Hermes built-in memory 会分裂。

**修正要求：**

- `on_memory_write(add/replace)` mirror 到 `/v1/remember`。
- `remove` 先只记录 event，不做 remote delete，除非已有 stable remote id mapping。
- metadata 中保留 `write_origin`、`execution_context`、`session_id`、`platform`，但过滤 secrets/reserved identity fields。
- 增加测试：内置 memory write 通过 MemoryManager fan-out 后，XMemo client 收到 remember。

### 2.10 P1 问题：生命周期 hooks 不完整

当前只实现了 `on_session_end()` 创建 snapshot。

缺少：

- `on_session_switch()`：更新 `_session_id`、清理 cache、记录 branch/reset/resume。
- `on_pre_compress()`：压缩前提取将被丢弃的高信号事实。
- `on_delegation()`：记录子代理任务结果。
- `agent_context` gating：非 primary / cron / subagent 默认不自动写入。

**修正要求：**

- Phase 1 加 `on_session_switch()`，解决 session cache 和 session id 正确性。
- Phase 2 加 `on_pre_compress()` 和 `on_delegation()`。
- `initialize()` 保存：

```python
self._agent_context = kwargs.get("agent_context", "primary")
self._auto_write_enabled = self._agent_context == "primary"
```

### 2.11 P1 问题：线程边界和 Hermes MemoryManager 重叠

Hermes `MemoryManager.sync_all()` 和 `queue_prefetch_all()` 已经把 provider work 放进单 worker background executor。

当前 XMemo provider 内部又自己创建：

- `xmemo-prefetch`
- `xmemo-sync`
- `xmemo-snapshot`

风险：

- `MemoryManager.flush_pending()` 只能等到 provider 方法返回，不能等 provider 内部线程完成。
- turn N / turn N+1 的写入顺序可能脱离 MemoryManager 串行保证。
- shutdown 时最多 join 当前两个线程，snapshot thread 没有保存引用。

**修正要求：**

- 首选：让 `sync_turn()` 在 MemoryManager worker 中执行 bounded REST，不再另开线程。
- `queue_prefetch()` 可同步执行 bounded recall 并写入 session cache，因为它已经在 MemoryManager worker 中。
- 如保留 provider 内部线程，必须用内部 queue + worker + shutdown drain，并让测试覆盖 flush。

### 2.12 P1 问题：prefetch 在 API call 路径可阻塞 3 秒

当前 `prefetch()` 会：

```python
self._prefetch_thread.join(timeout=3.0)
```

这会直接发生在 Hermes 组装当前 API call 前。若网络慢，每轮都可能增加 3 秒延迟。

**修正要求：**

- `prefetch()` 不等待网络；只消费已完成 cache。
- 如要短等，限制到 100-250ms，并只在首轮或明确配置开启。
- 失败或未完成直接返回空字符串。

### 2.13 P1 问题：CLI 未完成

当前 `plugins/memory/xmemo/cli.py` 实际是 setup helper，没有实现 Hermes memory plugin CLI discovery 需要的：

```python
register_cli(subparser)
xmemo_command(args)
```

因此计划中的：

```text
hermes xmemo status
hermes xmemo scope
hermes xmemo doctor
```

尚未完成。

**修正要求：**

- 外部插件实现 `register_cli(subparser)`。
- active provider 时暴露：
  - `hermes xmemo status`
  - `hermes xmemo doctor`
  - `hermes xmemo scope <scope>`
- `status` 输出必须 redacted，不显示 token。

### 2.14 P1 问题：文档存在 secret 和 profile 风险

当前 README 手动配置示例：

```bash
echo "XMEMO_KEY=your-token" >> ~/.hermes/.env
```

问题：

- 可能进入 shell history。
- 硬编码 `~/.hermes`，不 profile-safe。
- 不符合 Hermes 文档风格，应该推荐 `hermes memory setup xmemo` 或 Hermes profile-aware 路径。

**修正要求：**

- 文档主路径只写：

```bash
hermes memory setup xmemo
```

- 手动路径说明 `$HERMES_HOME/.env`，并提示不要把 token 放入 shell history / git / logs。
- 不建议用 echo 明文 token。

---

## 3. 修订后的目标架构

### 3.1 发布形态

推荐目标：

```text
xmemo-hermes-plugin/
├── README.md
├── pyproject.toml
├── plugin.yaml
├── xmemo/
│   ├── __init__.py
│   ├── client.py
│   ├── config.py
│   ├── capture.py
│   └── cli.py
└── tests/
    ├── test_provider.py
    ├── test_client_contract.py
    └── test_user_plugin_install.py
```

安装后落到：

```text
$HERMES_HOME/plugins/xmemo/
├── __init__.py
├── client.py
├── config.py
├── capture.py
├── cli.py
├── plugin.yaml
└── README.md
```

所有内部导入必须是相对导入。

### 3.2 默认工具面

默认工具只保留：

| Tool | 默认 | 说明 |
| --- | --- | --- |
| `xmemo_recall_context` | yes | 获取 bounded context pack |
| `xmemo_search` | yes | 语义搜索 |
| `xmemo_remember` | yes | 显式保存 durable memory |
| `xmemo_update_state` | yes | 保存 active task / next action / blocker |
| `xmemo_record_event` | opt-in | timeline event |
| `xmemo_create_reminder` | opt-in | reminder |
| `xmemo_list_reminders` | opt-in | reminder list |
| `xmemo_complete_reminder` | opt-in | complete reminder |
| `xmemo_create_pending_decision` | opt-in | pending decision |
| `xmemo_resolve_decision` | opt-in | resolve decision |
| `xmemo_create_restart_snapshot` | opt-in/tool or lifecycle | restart snapshot |
| `xmemo_restore_restart_snapshot` | opt-in | restore snapshot |
| `xmemo_mark_used` | internal first | recall feedback |
| `xmemo_forget` | off by default | destructive exact-id only |

### 3.3 配置模型

Secrets：

```text
XMEMO_KEY
MEMORY_OS_API_KEY  # compatibility fallback
```

Non-secret provider config：

```json
{
  "base_url": "https://xmemo.dev",
  "agent_id": "hermes",
  "agent_instance_id": "random-persisted-uuid",
  "bucket": "work",
  "scope": "hermes/default",
  "timeout_seconds": 5.0,
  "prefetch_max_items": 5,
  "prefetch_max_tokens": 900,
  "enable_workflow_tools": false,
  "enable_destructive_tools": false,
  "capture_timeline": false
}
```

要求：

- `api_key` 永远不从 JSON 读取。
- `agent_instance_id` 用随机 UUID 首次生成并持久化，不从 hostname/username 派生。
- `scope` 默认 `hermes/<profile>`。
- 支持 `$HERMES_HOME`，不硬编码 `~/.hermes`。

### 3.4 Capture 策略

默认写入来源：

- 显式 `xmemo_remember`。
- 显式 `xmemo_update_state`。
- Hermes built-in `memory` tool add/replace mirror。
- 高信号 turn，且通过 capture policy。

默认不写：

- 普通闲聊。
- 短 ACK。
- 每轮摘要。
- 原始 tool output。
- 未经过滤的日志、路径、token、错误全文。

---

## 4. 修订后的实施路线

### Phase P0：发布前阻断项修复

- [ ] 把 provider 从 in-tree 形态抽为外部插件形态。
- [ ] 所有 `plugins.memory.xmemo` 绝对导入改为相对导入。
- [ ] 增加 `$HERMES_HOME/plugins/xmemo` 加载测试。
- [ ] 修正 `mark_used()` endpoint：`/v1/memories/{memory_id}/usage`。
- [ ] 修正 `forget()` endpoint：`/v1/memories/{memory_id}/forget`。
- [ ] `xmemo_forget` 默认不暴露；启用时只接受 exact memory id。
- [ ] 移除 JSON `api_key` fallback。
- [ ] `is_available()` 不写 `xmemo.json`。
- [ ] prefetch cache 改为 per-session。
- [ ] 默认关闭 every-turn timeline write。
- [ ] README 移除 `echo XMEMO_KEY...` 明文 token 示例。
- [ ] 测试环境补 `.venv`/`venv`，用 `scripts/run_tests.sh` 跑相关测试。

P0 验收：

- [ ] 外部插件目录加载成功。
- [ ] 无 bundled 绝对导入。
- [ ] MockTransport 验证所有 REST path。
- [ ] 两个 session 的 prefetch 不串。
- [ ] 无 env key 时 provider 不可用，JSON key 不生效。
- [ ] 每轮普通对话不会自动写 timeline。
- [ ] `bash scripts/run_tests.sh tests/plugins/memory/test_xmemo.py tests/hermes_cli/test_memory_setup_provider_arg.py` 通过。

### Phase P1：Hermes lifecycle 对齐

- [ ] 实现 `on_memory_write()` mirror。
- [ ] 实现 `on_session_switch()`。
- [ ] 实现 `agent_context` gating。
- [ ] 改造线程边界，让 MemoryManager 的 executor 成为主要异步边界。
- [ ] `prefetch()` 不在 API call 路径长时间 join。
- [ ] tool 参数转换内部捕获 ValueError/TypeError，返回 JSON `tool_error`。
- [ ] `client.py` debug log 对 response body 做 redaction/truncation。
- [ ] `system_prompt_block()` 降低“每次先搜索”的倾向，避免过度 tool call。

P1 验收：

- [ ] built-in memory add/replace 能 mirror 到 XMemo。
- [ ] `/resume`、`/branch`、`/reset` 后 session id 和 cache 正确。
- [ ] subagent/cron 不自动写长期记忆。
- [ ] provider shutdown 能 drain 或安全放弃全部后台任务。

### Phase P2：CLI 与 workflow 工具

- [ ] 实现 `register_cli(subparser)`。
- [ ] 实现 `hermes xmemo status`。
- [ ] 实现 `hermes xmemo doctor`。
- [ ] 实现 `hermes xmemo scope <scope>`。
- [ ] workflow tools 受 `enable_workflow_tools` gate 控制。
- [ ] 加 pending decision tools：
  - `xmemo_create_pending_decision`
  - `xmemo_list_pending_decisions`
  - `xmemo_resolve_decision`
- [ ] 加 restart restore tool：
  - `xmemo_restore_restart_snapshot`
- [ ] `mark_used` 优先作为自动反馈闭环，不默认要求模型手动调用。

### Phase P3：高信号 capture 与压缩/委派

- [ ] 新增 `capture.py`，实现 deterministic high-signal policy。
- [ ] `sync_turn()` 使用 capture policy，而不是 every-turn event。
- [ ] `on_pre_compress()` 提取压缩前重要事实。
- [ ] `on_delegation()` 记录子代理任务和结果摘要。
- [ ] `on_session_end()` 创建 restart snapshot，但要尊重配置和 rate limit。
- [ ] 引入 XMemo `usage_tracking_id` 闭环：search/recall 返回 id，使用后自动 `/usage`。

### Phase P4：Ledger 与跨仓库工具

- [ ] 验证 XMemo ledger 的 REST 投影：
  - `GET /v1/me/ledger/transactions`
  - `GET /v1/me/ledger/monthly-summary`
- [ ] `xmemo_add_expense` 不直接假设专用 REST endpoint；优先用 `/v1/remember` + ledger metadata，或等待官方 ledger write REST helper。
- [ ] 如果暴露财务工具，必须默认 opt-in，并加入 schema 描述限制。
- [ ] 更新 `memory-os` 的 Hermes adapter/docs。
- [ ] 更新 XMemo CLI 的 `setup hermes`，但 CLI 只辅助配置，不替代 Hermes provider。

---

## 5. 测试计划

必须使用 Hermes wrapper：

```bash
bash scripts/run_tests.sh tests/plugins/memory/test_xmemo.py
bash scripts/run_tests.sh tests/hermes_cli/test_memory_setup_provider_arg.py
```

当前本机验证结果：

```text
error: no virtualenv found in /mnt/h/repos/hermes-agent/.venv or /mnt/h/repos/hermes-agent/venv
```

因此本次审核不能声称测试通过。

### 5.1 必补测试

- 外部插件加载：
  - temp `$HERMES_HOME/plugins/xmemo`
  - `load_memory_provider("xmemo")`
  - 相对导入不失败
- REST contract：
  - `httpx.MockTransport`
  - assert path/method/payload/header
  - 特别覆盖 `/usage` 和 `/forget`
- Session isolation：
  - session A queue recall A
  - session B queue recall B
  - A/B prefetch 各自消费
- Secret behavior：
  - JSON 中存在 `api_key` 不生效
  - `save_config()` 清除 JSON secret
- No side-effect availability：
  - `is_available()` 不创建 `xmemo.json`
- Capture policy：
  - trivial prompt 不写
  - explicit remember 写
  - decision/high-signal 写
- Built-in memory bridge：
  - `MemoryManager.on_memory_write("add", ...)` 调到 XMemo remember
- Tool gate：
  - workflow/destructive off 时 schema 不出现
  - on 时 schema 出现

---

## 6. 与 XMemo REST 合约的目标映射

| XMemo 能力 | REST | Hermes 默认策略 |
| --- | --- | --- |
| recall context | `POST /v1/recall/context` | 默认启用 |
| search | `GET /v1/memories/search` | 默认启用 |
| remember | `POST /v1/remember` | 默认启用 |
| update state | `POST /v1/update_state` | 默认启用 |
| timeline event | `POST /v1/timeline/events` | opt-in / high-signal |
| timeline query | `GET /v1/timeline` | opt-in |
| reminders | `POST/GET /v1/reminders` | opt-in workflow |
| complete reminder | `POST /v1/reminders/{id}/complete` | opt-in workflow |
| pending decisions | `POST/GET /v1/decisions` | opt-in workflow |
| resolve decision | `POST /v1/decisions/{id}/resolve` | opt-in workflow |
| restart snapshot | `POST /v1/restart/snapshot` | lifecycle / opt-in |
| restart restore | `POST /v1/restart/restore` | opt-in |
| mark used | `POST /v1/memories/{id}/usage` | internal feedback first |
| forget | `POST /v1/memories/{id}/forget` | destructive opt-in |
| ledger read | `GET /v1/me/ledger/*` | Phase P4 |
| ledger write | `/v1/remember` + ledger metadata or future helper | Phase P4 |

---

## 7. 官方匹配标准

可以宣称 “XMemo 完美匹配 Hermes” 前，必须全部满足：

- [ ] 外部插件发布形态通过，不需要 PR 新增 `plugins/memory/xmemo/`。
- [ ] 安装到 `$HERMES_HOME/plugins/xmemo/` 后能工作。
- [ ] 不改 Hermes 核心文件，除非是通用 plugin surface 修复。
- [ ] `is_available()` 无网络、无写文件副作用。
- [ ] system prompt 稳定，不注入动态 recall。
- [ ] prefetch 只影响当前 API call，不污染 `messages`。
- [ ] prefetch cache 按 session 隔离。
- [ ] 默认工具面精简。
- [ ] destructive 工具默认关闭。
- [ ] 自动写入遵守高信号 capture，不 every-turn 记录。
- [ ] secrets 不进 JSON、日志、README shell history 示例。
- [ ] profile 默认隔离。
- [ ] REST endpoints 与 Memory OS 官方 client/API 一致。
- [ ] Hermes wrapper 测试通过。
- [ ] Live E2E 在显式 `XMEMO_LIVE_TEST=1` 下通过。

---

## 8. 推荐下一步

最小修复 PR / patch 顺序：

1. 修 `client.py` endpoint：`usage` / `forget`。
2. 修 `config.py`：secret fallback、`is_available()` 副作用、random persisted instance id。
3. 修 `__init__.py`：relative imports、per-session prefetch cache、关闭 every-turn timeline write。
4. 缩默认工具面并 gate workflow/destructive tools。
5. 实现 `on_memory_write()` 和 `on_session_switch()`。
6. 抽成 `$HERMES_HOME/plugins/xmemo` 外部插件形态并补加载测试。
7. 补 `.venv` 后用 `scripts/run_tests.sh` 跑相关测试。

完成以上后，XMemo 才从“可运行 spike”进入“符合 Hermes 官方插件精神的 provider”。

---

## 9. 审核参考文件

Hermes：

- `AGENTS.md`
- `agent/memory_provider.py`
- `agent/memory_manager.py`
- `agent/agent_init.py`
- `agent/turn_context.py`
- `agent/conversation_loop.py`
- `agent/turn_finalizer.py`
- `agent/agent_runtime_helpers.py`
- `agent/tool_executor.py`
- `plugins/memory/__init__.py`
- `plugins/memory/xmemo/__init__.py`
- `plugins/memory/xmemo/client.py`
- `plugins/memory/xmemo/config.py`
- `plugins/memory/xmemo/cli.py`
- `plugins/memory/xmemo/plugin.yaml`
- `plugins/memory/xmemo/README.md`
- `tests/plugins/memory/test_xmemo.py`

Memory OS / XMemo：

- `docs/API_SPEC.md`
- `docs/AGENT_INTEGRATION.md`
- `src/memory_manager/client.py`
- `src/memory_manager/models/requests.py`
- `src/memory_manager/routes/action_items.py`
- `src/memory_manager/routes/memory_write.py`
- `src/memory_manager/routes/recall.py`
- `src/memory_manager/routes/me.py`

