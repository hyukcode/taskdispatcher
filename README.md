# tasker — 多智能体任务编排器（Claude Agent SDK × Codex App Server）

输入一个目标，由 LLM 把它拆成**有依赖关系的子任务**，分派给 **Claude Agent SDK** 和 **Codex App Server** 并行/串行执行；CLI 按层级展示文本、工具调用、交互、审批和最终结果，并支持中途注入消息。

> macOS、Windows、Linux 均支持 headless 任务编排；执行器统一通过 SDK / App Server 接入。

---

## 特性

| 能力 | 说明 |
|---|---|
| 🧠 分层事件 | 默认显示核心事件；`display.level=verbose` 增加 thinking/system/usage；`debug` 显示 raw |
| 🔧 工具调用 | `tool_use`（工具名 + 参数 JSON）与 `tool_result`（stdout/stderr/是否报错） |
| 🔁 交互事件 | 子代理启动/进度/通知、任务轮次 |
| 🛡️ 审批请求 | 权限拒绝、`permission_request`/`permission_result` 实时浮现；`approval` 策略可 auto/log/ask |
| 💬 中途修改 | 运行中 `@claude/@codex/@all <消息>` 注入当前 SDK / App Server 会话 |
| 🎯 依赖编排 | LLM 拆出依赖图 → 分层并发；上游输出自动作为下游上下文 |
| 🧭 自适应拆分 | 有模板时按模板编排；无模板时用 ReAct 风格多轮拆分 |
| 🛟 降级执行 | 拆分 LLM 不可用时，将完整目标交给一个 code agent 执行 |

---

## 安装

```bash
# 1) 准备两个执行器
claude                 # Claude Agent SDK 使用的 Claude Code CLI
npm install -g @openai/codex   # Codex App Server

# 2) 安装 tasker（命令名 tasker，PyPI 包名 multicc）
pip install multicc                    # 从 PyPI（需联网）
# 或离线：源码机 python scripts/build_wheel.py 得到 .whl 后 pip install <whl>
# 或源码直接跑：python tasker_cli.py

tasker verify-config     # 检查环境

# 3) 配置（可选项，见下方"配置"）
tasker init && $EDITOR config.json
```

Python ≥ 3.9。完整打包 / 发布说明见 [RELEASE.md](RELEASE.md)。

---

## 快速开始

```bash
# 进入 REPL（LLM 拆分 → claude/codex 执行 → CLI 实时输出）
python -m tasker repl

# 只打印计划
python -m tasker plan "重构这个项目" --json

```

### 运行中随时输入

```
@all <消息>          发给所有运行中的 agent
@claude <消息>       只发给 claude 执行的任务
@codex <消息>        只发给 codex 执行的任务
@t2 <消息>           发给指定任务
<裸文本>              等同 @all <文本>
:allow [<id>]        批准最近/指定审批请求
:deny  [<id>]        拒绝最近/指定审批请求
:done  [<taskid>]    手动收尾任务（关闭其 stdin 让进程退出）
:status              各任务进度（思考/工具/审批/注入计数）
:plan                重打计划
:stop /stop          中止当前任务并返回 tasker>（可用 /continue 继续）
:quit                终止全部并退出
```

> Claude 子任务使用持久的 SDK 会话；Codex 子任务使用持久的 App Server thread，运行中注入的消息会进入当前会话。

执行结束后，结果区会先列出每个子任务的状态、耗时、尝试次数、工作目录、交付结果和错误，再显示总体 goal 结果。空闲提示符 `tasker>` 下可使用：

```
/continue              继续当前 tasker 会话；复用成功任务，重新执行未完成任务
/resume <session_id>   恢复指定的中断会话
/restart <task_id>     从指定子任务重新执行，并重跑它的所有下游任务
/status                查看当前会话的子任务状态
/new <目标>             新建一个独立任务会话
```

`/continue` 和 `/restart` 会复用相同的 tasker session、任务工作区、成功任务输出和事件日志，但会为重新执行的任务创建新的 runner 尝试；它们不是把已经结束的 Claude SDK 对话或 Codex turn 原样重新连接。运行中的消息注入仍使用 `@t2`、`@claude`、`@codex` 或 `@all`。

`/restart t3` 会保留 `t3` 的上游成功任务，只清除 `t3` 及其下游任务的当前快照，然后按依赖顺序重跑；事件日志和历史记录仍保留。`/delete <session_id>` 只标记会话为已删除，保留计划、事件日志和工作区，可用 `/restore <session_id>` 恢复。

---

## 架构

```
prompt
  │  LLM API（anthropic / openai 兼容）
  ▼
planner ──► Plan {tasks[], depends_on[], executor: claude|codex}
  │            ├─ 有模板：模板编排 LLM
  │            ├─ 无模板：ReAct（候选 → 复核 → 收敛）
  │            └─ LLM 不可用：单个 code agent 执行完整目标
  ▼
 GoalLoop（外层 goal 收敛）
   └─► GraphExecutor（按依赖分层，max_parallel 并发）
        ├─► SdkClaudeRunner ─► Claude Agent SDK 会话
        │        │             SDK query → receive_response → @all/@claude 注入
        │        ▼
        │     消息: System / Assistant / User / Result + blocks
        ├─► CodexAppServerRunner ─► codex app-server（JSON-RPC）
        │        │             approval_request / tool_call / reasoning / completed
        │        ▼
        ├─► session workspace ─► 中间产物、读取结果和会话事件
        │    同层仅显式 workspace_access=read_only 的任务并发，含写任务的层串行
        └─► repository tasks ─► tasker 启动目录（无代码时由用户确认仓库目录）
             事件写入 ~/.tasker/sessions/<session_id>/events/<task>.events.jsonl
            │
            ▼
      用户在终端输入 → 输入线程 → 路由给对应 runner
```

事件模型（`tasker/models.py`）：
`thinking` `text` `tool_use` `tool_result` `permission_request` `permission_result` `user_message` `interaction` `usage` `system` `error` `result` `raw`

文件结构：

```
tasker/
  main.py          CLI 入口（repl / plan / verify-config / init）
  repl.py          REPL、session 生命周期和运行中输入路由
  goal_loop.py     外层 goal loop 与 session workspace
  graph_executor.py 依赖分层、并发、任务生命周期和审批接线
  planner.py       模板编排 / ReAct 拆分 / 单 agent 降级 + 依赖校验/环检测
  sdk_runner.py    Claude Agent SDK 事件采集 + 会话注入 + 完成判定
  codex_app_server_runner.py  Codex App Server 事件采集 + thread/turn 注入
  live.py          LiveTui：实时打印 + 后台输入线程 + 指令解析
  approvals.py     审批策略（auto / log / ask_console）
  llm.py / config.py / spawn.py / models.py / console.py
```

`SubTask.workspace_access` 可取 `read_only` 或 `write`，由模板或规划 LLM 显式声明；缺失或非法值统一按 `write` 处理，不再根据关键词推断。同层只有纯只读任务允许并发，避免多个 agent 同时覆盖文件。`SubTask.workdir_scope` 可取 `session` 或 `repository`，也由模板或规划 LLM 显式声明；缺失或非法值统一按 `session` 处理，旧模板中的 `repository` 布尔字段仍作为兼容格式读取。`SubTask.executor` 同样只使用显式的 `claude`、`codex` 或 `human`，缺失或非法值使用配置默认 executor，不再根据任务文本改写。tasker 启动目录没有代码文件时，进入 REPL 前会询问仓库目录。

---

## 配置（config.json）

| 段 | 键 | 默认 | 说明 |
|---|---|---|---|
| `llm` | `provider` | `anthropic` | 拆分用 LLM；`openai` 可接任意 OpenAI 兼容接口（DeepSeek/Ollama/网关） |
| `llm` | `base_url` | `""` | 兼容接口地址，如 `https://api.deepseek.com/v1` |
| `llm` | `api_key_env` | `ANTHROPIC_API_KEY` | 从该环境变量取 key |
| `llm` | `model` | `claude-sonnet-5` | 拆分模型 |
| `claude` | `permission_mode` | `acceptEdits` | headless 下自动允许工作区内编辑；要全自主（含 Bash）改 `bypassPermissions` |
| `claude` | `allowed_tools` | `[]` | 预授权工具白名单 |
| `claude` | `completion_idle` | `5.0` | 最终 result 后无活动秒数 → 关闭 stdin 收尾 |
| `codex` | `sandbox` | `workspace-write` | `read-only` / `workspace-write` / `danger-full-access` |
| `codex` | `approval_policy` | `on-request` | Codex App Server 的审批策略 |
| `session` | `workspace_dir` | `~/.tasker/workspace` | session 中间产物目录；代码任务改用用户确认的 repository 目录 |
| `display` | `level` | `minimal` | `minimal` 核心事件；`verbose` 详情；`debug` 原始协议事件 |
| `approval` | `mode` | `auto` | `auto` / `log` / `ask_console` |
| `approval` | `default_allow` | `true` | auto 模式的默认决定 |
| `retry` | `max_retries` | `1` | 同一 executor 对临时/超时/协议失败的最大重试次数 |
| `retry` | `initial_delay` / `max_delay` | `1` / `30` | 指数退避的首个和最大等待秒数 |
| `dispatch` | `failover_enabled` | `true` | Claude/Codex 任务失败时是否切换另一 agent 重试 |
| `dispatch` | `max_failover_attempts` | `1` | 每个任务最多允许的备用 agent 尝试次数；`0` 表示关闭故障转移 |
| `review` | `enabled` | `false` | 是否启用独立 reviewer + 证据验证阶段 |
| `review` | `reviewer_count` / `min_confidence` | `2` / `80` | reviewer 数量（最多 3 个角度）和验证通过置信度阈值 |
| `tools` | `entries` | `[]` | 注册额外工具元数据；只描述能力，不执行配置中的命令 |
| `hooks` | `rules` | `[]` | 工具调用前/后的字符串、路径策略；支持 `warn` / `block` |
| `runtime` | `*_queue_maxsize` / `max_*_chars` | 见示例 | 限制事件、输入队列和注入上下文，避免长任务无限增长 |
| — | `max_parallel` | `2` | 同层并发数 |
| — | `timeout_per_task` | `900` | 单任务超时 |

---

## 执行器行为说明

- **Claude**：由 `claude-agent-sdk` 管理会话、工具调用和权限回调。
- **Codex**：由 `codex app-server --listen stdio://` 管理 thread/turn、事件流和审批请求。
- **权限**：两种执行器都通过统一的 `permission_request` / `permission_result` 事件接入审批策略。
- **工具发现**：planner 和 worker 会从有界的 `ToolCatalog` 中按提示搜索工具；搜索结果仍需经过 executor、读写权限、工作目录、allow/deny 和审批策略校验。
- **故障转移**：先对临时、超时和协议失败做有限指数退避，再按策略切换另一种 agent；每次尝试保存 `attempt_id`、父尝试、失败分类和转换原因。人工审查拒绝、权限拒绝和主动停止不会通过切换 agent 绕过。
- **独立审查**：`review.enabled=true` 时，代码任务结束后运行多个只读审查角度，再由验证 reviewer 核对文件/测试证据；不通过会进入下一轮目标循环。
- **阶段工作流**：模板可选 `workflow.stages`，Tasker 会增加阶段屏障和阶段事件，但不会改写模板目标、任务标题、描述、验收标准或任务数量。
- **编码**：全程 UTF-8；Windows、macOS、Linux 均使用 headless 事件采集。
- **SIGINT**：`Ctrl-C` 触发优雅中断。

## Codex App Server 的说明

- Codex 任务使用持久 thread；`@codex` 注入通过 `turn/steer` 或新的 `turn/start` 进入当前任务。
- 未安装或版本不支持 App Server 时，含 Codex 的任务会失败并给出错误；拆分 LLM 不可用时会降级为单个 code agent 任务。

---

## 常见问题

**任务拆分 LLM 报 401 / 网络错？**
`llm` 段用独立 API key。若你的 `ANTHROPIC_API_KEY` 是 Claude Code 网关专用 key，无法直接访问 api.anthropic.com —— 设 `llm.base_url` 指向你的网关，或换 `provider=openai` + DeepSeek/Ollama。拆分 LLM 失败时不会再猜测拆分，而是将完整目标交给一个 code agent。

**headless 下"审批请求"不弹窗？**
审批请求会进入 Tasker 的统一事件流；`approval.mode=ask_console` 时，在当前 REPL 终端输入 `:allow` 或 `:deny`。

**运行卡住不结束？**
执行器发完最终结果后，Tasker 根据 `result` / `turn/completed` 和 `completion_idle` 收尾；可 `:done` 手动收尾。
# taskdispatcher
