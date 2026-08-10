"""tasker —— 多智能体任务编排器。

通过 LLM API 把用户 prompt 拆分为子任务，分派给 Claude Code 与 Codex 执行，
并完整采集两者的"思维链 / 工具调用 / 交互 / 审批请求"事件，输出 Markdown 报告。
"""

__version__ = "0.1.2"
