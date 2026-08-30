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
| 🧪 零依赖 | 纯 Python 标准库；`--mock` 无需任何 CLI/API key 即可演示全流程 |
| 📋 报告 | 可选 `--report` 落一份完整 Markdown 轨迹 |

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
# 真实运行（LLM 拆分 → claude/codex 执行 → CLI 实时输出）
python -m tasker run "写一个 Python CLI 工具，带子命令，并补上单元测试"

# 规则拆分（不用 LLM 也能跑，适合快速试）
python -m tasker run "实现一个斐波那契脚本" --plan-rules

# 无任何 CLI / API key 的纯演示
python -m tasker run "写一个 python 脚本并测试" --mock --think head

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
:quit                终止全部并退出
```

> Claude 子任务使用持久的 SDK 会话；Codex 子任务使用持久的 App Server thread，运行中注入的消息会进入当前会话。

---

## 架构

```
prompt
  │  LLM API（anthropic / openai 兼容）
  ▼
planner ──► Plan {tasks[], depends_on[], executor: claude|codex}
  │            │  (规则拆分 plan_with_rules 作为无 key 回退)
  ▼
scheduler（按依赖分层，max_parallel 并发）
  ├─► SdkClaudeRunner ─► Claude Agent SDK 会话
  │        │             SDK query → receive_response → @all/@claude 注入
  │        ▼
  │     消息: System / Assistant / User / Result + blocks
  ├─► CodexAppServerRunner ─► codex app-server（JSON-RPC）
  │        │             approval_request / tool_call / reasoning / completed
  │        ▼
  └─► 每个事件 → LiveTui 实时打印 + 写入 workspaces/<run>/<task>.events.jsonl
            │
            ▼
      用户在终端输入 → 输入线程 → 路由给对应 runner
```

事件模型（`tasker/models.py`）：
`thinking` `text` `tool_use` `tool_result` `permission_request` `permission_result` `user_message` `interaction` `usage` `system` `error` `result` `raw`

文件结构：

```
tasker/
  main.py          CLI 入口（run / plan / verify-config / init）
  scheduler.py     依赖分层、并发、用户输入路由、审批策略接线
  planner.py       LLM 拆分 + 规则回退 + 依赖校验/环检测
  sdk_runner.py    Claude Agent SDK 事件采集 + 会话注入 + 完成判定
  codex_app_server_runner.py  Codex App Server 事件采集 + thread/turn 注入
  live.py          LiveTui：实时打印 + 后台输入线程 + 指令解析
  approvals.py     审批策略（auto / log / ask_console）
  llm.py / config.py / spawn.py / models.py / console.py / report.py / mock_runner.py
```

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
| `display` | `level` | `minimal` | `minimal` 核心事件；`verbose` 详情；`debug` 原始协议事件 |
| `approval` | `mode` | `auto` | `auto` / `log` / `ask_console` |
| `approval` | `default_allow` | `true` | auto 模式的默认决定 |
| — | `max_parallel` | `2` | 同层并发数 |
| — | `timeout_per_task` | `900` | 单任务超时 |

---

## 执行器行为说明

- **Claude**：由 `claude-agent-sdk` 管理会话、工具调用和权限回调。
- **Codex**：由 `codex app-server --listen stdio://` 管理 thread/turn、事件流和审批请求。
- **权限**：两种执行器都通过统一的 `permission_request` / `permission_result` 事件接入审批策略。
- **编码**：全程 UTF-8；Windows、macOS、Linux 均使用 headless 事件采集。
- **SIGINT**：`Ctrl-C` 触发优雅中断。

## Codex App Server 的说明

- Codex 任务使用持久 thread；`@codex` 注入通过 `turn/steer` 或新的 `turn/start` 进入当前任务。
- 未安装或版本不支持 App Server 时，含 Codex 的任务会失败并给出错误；可用 `--mock` 或 `--plan-rules` 先体验。

---

## 常见问题

**任务拆分 LLM 报 401 / 网络错？**
`llm` 段用独立 API key。若你的 `ANTHROPIC_API_KEY` 是 Claude Code 网关专用 key，无法直接访问 api.anthropic.com —— 设 `llm.base_url` 指向你的网关，或换 `provider=openai` + DeepSeek/Ollama。任何失败都会自动回退到规则拆分。

**headless 下"审批请求"不弹窗？**
审批请求会进入 Tasker 的统一事件流；`approval.mode=ask_console` 时，在当前 REPL/run 终端输入 `:allow` 或 `:deny`。

**运行卡住不结束？**
执行器发完最终结果后，Tasker 根据 `result` / `turn/completed` 和 `completion_idle` 收尾；可 `:done` 手动收尾。
# taskdispatcher
