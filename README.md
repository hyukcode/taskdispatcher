# tasker — 多智能体任务编排器（claude code × codex）

输入一个目标，由 LLM 把它拆成**有依赖关系的子任务**，分派给 **Claude Code** 和 **Codex** 并行/串行执行；在 CLI 里**实时流式输出两者的完整思维链、工具调用、交互与审批请求**，并且你可以随时打字**继续提要求、中途修改、批准/拒绝**。

> 主目标平台：**macOS**。Windows/Linux 也可运行（headless 采集相同），但 pty 交互 attach 仅 macOS/Linux 原生支持。

---

## 特性

| 能力 | 说明 |
|---|---|
| 🧠 完整思维链 | Claude 的 `thinking` 块、Codex 的 `reasoning` 逐条实时打印（`--think full/head/off`） |
| 🔧 工具调用 | `tool_use`（工具名 + 参数 JSON）与 `tool_result`（stdout/stderr/是否报错） |
| 🔁 交互事件 | 子代理启动/进度/通知、任务轮次 |
| 🛡️ 审批请求 | 权限拒绝、`permission_request`/`permission_result` 实时浮现；`approval` 策略可 auto/log/ask |
| 💬 中途修改 | 运行中 `@claude/@codex/@all <消息>` 实时注入（claude 为真·多轮注入；codex 见下方限制） |
| 🎯 依赖编排 | LLM 拆出依赖图 → 分层并发；上游输出自动作为下游上下文 |
| 🧪 零依赖 | 纯 Python 标准库；`--mock` 无需任何 CLI/API key 即可演示全流程 |
| 📋 报告 | 可选 `--report` 落一份完整 Markdown 轨迹 |

---

## 安装

```bash
# 1) 准备两个执行器（macOS）
claude                 # Claude Code CLI（本工具直接调用）
npm install -g @openai/codex   # Codex CLI（可选，未装时 codex 任务会失败/可先用 --mock）

# 2) 安装 tasker（命令名 tasker，PyPI 包名 multicc）
pip install multicc                    # 从 PyPI（需联网）
# 或离线：源码机 python scripts/build_wheel.py 得到 .whl 后 pip install <whl>
# 或源码直接跑：python tasker_cli.py

tasker verify-config     # 检查环境

# 3) 配置（可选项，见下方"配置"）
tasker init && $EDITOR config.json
```

Python ≥ 3.9，无第三方依赖。完整打包 / 发布说明见 [RELEASE.md](RELEASE.md)。

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

# 附加到交互 TUI（macOS 原生，直接看真实审批弹窗）
python -m tasker attach claude "帮我初始化一个 Go 项目"
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

> claude 子任务是**真·实时多轮**：注入的消息写入其 `stream-json` stdin，下一轮即被采纳（已实测：任务执行到一半注入"第二行改成 xxx"，最终产物按修改后内容产出）。

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
  ├─► ClaudeRunner ─► claude -p --output-format stream-json --verbose --input-format stream-json
  │        │             stdin 保持打开 → @all/@claude 实时注入
  │        ▼
  │     事件流: thinking / tool_use / tool_result / task_* / result / permission_denials
  ├─► CodexRunner ─► codex exec --json --full-trace
  │        │             approval_request / tool_call / reasoning / completed
  │        ▼
  └─► 每个事件 → LiveTui 实时打印 + 写入 workspaces/<run>/<task>.events.jsonl
            │
            ▼
      用户在终端输入 → 输入线程 → 路由给对应 runner
```

事件模型（`tasker/models.py`）：
`thinking` `text` `tool_use` `tool_result` `permission_request` `permission_result` `user_message` `interaction` `system` `error` `result` `raw`

文件结构：

```
tasker/
  main.py          CLI 入口（run / plan / attach / verify-config / init）
  scheduler.py     依赖分层、并发、用户输入路由、审批策略接线
  planner.py       LLM 拆分 + 规则回退 + 依赖校验/环检测
  claude_runner.py Claude 事件采集 + stdin 实时注入 + 完成判定
  codex_runner.py  Codex 事件采集
  live.py          LiveTui：实时打印 + 后台输入线程 + 指令解析
  approvals.py     审批策略（auto / log / ask_console）
  ptty.py          pty attach（macOS/Linux，真实审批交互）
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
| `codex` | `auto_approve` | `false` | 传 `--auto-approve` 给 codex exec |
| `approval` | `mode` | `auto` | `auto` / `log` / `ask_console` |
| `approval` | `default_allow` | `true` | auto 模式的默认决定 |
| — | `max_parallel` | `2` | 同层并发数 |
| — | `timeout_per_task` | `900` | 单任务超时 |

---

## macOS 行为说明（重点）

- **进程启动**：macOS 上 `claude`/`codex` 是 POSIX 二进制或 `#!/bin/sh` shim，`spawn.py` 直接用 `shutil.which` 解析后 `Popen`，无需 `.cmd` 兼容层（Windows 的 `.cmd` 已额外处理）。
- **交互 attach**：`python -m tasker attach claude|codex` 用 `os.forkpty` 起交互式 TUI 并原样转发到你终端 —— 你能看到**真实的审批弹窗**并直接输入 `y/n` 回答。这是 headless 拿不到的那部分交互（claude -p / codex exec 无 TTY 不弹窗）。
- **编码**：全程 UTF-8；macOS 终端默认 UTF-8，中文/emoji 无碍。
- **权限**：headless 下 claude 的写文件/命令会走策略判定，拒绝会以 `tool_result` 拒由 + `result.permission_denials` 呈现（已实测）；需要"不问就干"请设 `permission_mode=bypassPermissions`（风险自负）。
- **SIGINT**：`Ctrl-C` 触发优雅中断（`KeyboardInterrupt` 路径）。

## Codex 的已知限制

- `codex exec` 是**单轮非交互**模式，**不支持**像 claude 那样中途注入消息。`@codex` 注入会记录到交互日志，并在调度**依赖它的下一个子任务**时作为上下文携带（"下一轮注入"）。
- `codex exec --json` 会发 `approval_request` 事件（本工具会实时打印），但非交互下无法经 stdin 回答；真正的审批对话请用 `attach codex`。
- 未安装 codex 时，含 codex 的任务会失败并给出清晰报错；可用 `--mock` 或 `--plan-rules` 先体验。

---

## 常见问题

**任务拆分 LLM 报 401 / 网络错？**
`llm` 段用独立 API key。若你的 `ANTHROPIC_API_KEY` 是 Claude Code 网关专用 key，无法直接访问 api.anthropic.com —— 设 `llm.base_url` 指向你的网关，或换 `provider=openai` + DeepSeek/Ollama。任何失败都会自动回退到规则拆分。

**headless 下"审批请求"不弹窗？**
这是 CLI 行为限制：权限弹窗只在交互 TTY 出现。headless 会直接给出工具执行/拒绝事件。要看并回答真实审批，用 `attach` 模式。

**运行卡住不结束？**
claude 发完最终结果不会自己退出（stdin 仍开）。完成判定：最终 `result` 后 `completion_idle` 秒无活动 → 关闭 stdin 收尾；可 `:done` 手动收尾。
# taskdispatcher
