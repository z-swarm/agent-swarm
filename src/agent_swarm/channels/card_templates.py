"""
@module agent_swarm.channels.card_templates
@brief  5 个内置飞书卡片模板——DESIGN §4.4 send_card()

W10-4 范围：
  - task_progress       任务进度看板
  - code_review_result  代码审查结果
  - adversarial_debug   对抗式调试进度
  - swarm_status        Swarm 运行状态
  - confirm_dialog      确认对话框（Human-in-the-loop）

@note 模板渲染函数签名一致: render_xxx(data: dict) -> dict
      返回的是 Lark 卡片 payload（结构 + 元素 + 动作）
@note 真实场景可被自定义模板覆盖——通过 LarkConnector.send(card_template=..., card_data=...)
"""

from __future__ import annotations

from typing import Any

# 卡片头部颜色
_HEADER_TEMPLATES: dict[str, str] = {
    "info": "blue",
    "success": "green",
    "warning": "orange",
    "error": "red",
    "neutral": "grey",
}


def _header(title: str, level: str = "info") -> dict[str, Any]:
    """@brief 卡片头部：标题 + 颜色"""
    return {
        "title": {
            "tag": "plain_text",
            "content": title,
        },
        "template": _HEADER_TEMPLATES.get(level, "blue"),
    }


def _div(text: str) -> dict[str, Any]:
    """@brief 简单文本块（支持 markdown）"""
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": text,
        },
    }


def _field(name: str, value: str) -> dict[str, Any]:
    """@brief 字段对（name: value）"""
    return {
        "tag": "div",
        "fields": [
            {
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": f"**{name}**\n{value}",
                },
            },
        ],
    }


def _actions(items: list[dict[str, str]]) -> dict[str, Any]:
    """@brief 按钮组（每项含 text/value/type）"""
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": item["text"]},
                "type": item.get("type", "default"),
                "value": {"action": item["value"], "label": item["text"]},
            }
            for item in items
        ],
    }


def _progress_bar(done: int, total: int) -> str:
    """@brief 文本进度条（无 native 进度条时降级）"""
    if total <= 0:
        return "0/0"
    pct = min(100, int(done * 100 / total))
    blocks = int(pct / 10)
    return f"[{'█' * blocks}{'░' * (10 - blocks)}] {done}/{total} ({pct}%)"


# ---------------------------------------------------------------------------
# 1) task_progress
# ---------------------------------------------------------------------------


def render_task_progress(data: dict[str, Any]) -> dict[str, Any]:
    """
    任务进度看板

    @param data {
        "title":       "Build Pipeline",
        "level":       "info|success|warning|error",
        "tasks":       [{"id": "T1", "title": "...", "status": "in_progress"}],
        "agent_count": int,
    }
    """
    tasks = data.get("tasks", [])
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    total = len(tasks)
    elements: list[dict[str, Any]] = [
        _div(f"**Overall**: {_progress_bar(completed, total)}  (failed: {failed})"),
        _div(f"**Agents**: {data.get('agent_count', 0)}"),
    ]
    # 每个 task 一行
    for t in tasks[:10]:  # 最多 10 个
        status_emoji = {
            "completed": "✅",
            "failed": "❌",
            "in_progress": "🔄",
            "pending": "⏳",
            "blocked": "🚧",
        }.get(t.get("status"), "•")
        elements.append(_div(f"{status_emoji} `{t.get('id', '?')}` {t.get('title', '?')}"))
    if len(tasks) > 10:
        elements.append(_div(f"... and {len(tasks) - 10} more"))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(data.get("title", "Task Progress"), data.get("level", "info")),
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# 2) code_review_result
# ---------------------------------------------------------------------------


def render_code_review_result(data: dict[str, Any]) -> dict[str, Any]:
    """
    代码审查结果

    @param data {
        "title":   "PR #123",
        "level":   "info|success|warning|error",
        "findings": [{"severity": "high", "file": "x.py", "line": 10, "msg": "..."}],
        "verdict": "approve|request_changes|comment",
    }
    """
    findings = data.get("findings", [])
    sev_count: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "low")
        sev_count[sev] = sev_count.get(sev, 0) + 1
    elements: list[dict[str, Any]] = [
        _field("Verdict", data.get("verdict", "comment")),
        _field("Critical", str(sev_count["critical"])),
        _field("High", str(sev_count["high"])),
        _field("Medium", str(sev_count["medium"])),
        _field("Low", str(sev_count["low"])),
    ]
    # 列出 high 以上
    for f in findings:
        if f.get("severity") in ("critical", "high"):
            elements.append(
                _div(
                    f"⚠️ **{f.get('severity', '?')}** `{f.get('file', '?')}:"
                    f"{f.get('line', '?')}` — {f.get('msg', '?')}"
                )
            )
    if not any(f.get("severity") in ("critical", "high") for f in findings):
        elements.append(_div("✅ No high-severity findings"))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(data.get("title", "Code Review"), data.get("level", "info")),
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# 3) adversarial_debug
# ---------------------------------------------------------------------------


def render_adversarial_debug(data: dict[str, Any]) -> dict[str, Any]:
    """
    对抗式调试进度（W8）

    @param data {
        "title":      "Debug Session",
        "round_no":   int,
        "max_rounds": int,
        "survivors":  [str],  # 存活假设 id 列表
        "convergence_reason": "..." | None,
    }
    """
    elements: list[dict[str, Any]] = [
        _field("Round", f"{data.get('round_no', 0)} / {data.get('max_rounds', 0)}"),
        _field("Survivors", str(len(data.get("survivors", [])))),
        _div(f"**Active hypotheses**: {', '.join(data.get('survivors', [])) or '(none)'}"),
    ]
    if data.get("convergence_reason"):
        elements.append(_div(f"**Converged**: {data['convergence_reason']}"))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(data.get("title", "Adversarial Debug"), "info"),
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# 4) swarm_status
# ---------------------------------------------------------------------------


def render_swarm_status(data: dict[str, Any]) -> dict[str, Any]:
    """
    Swarm 运行状态

    @param data {
        "title":  "MySwarm",
        "state":  "running|completed|failed",
        "agents": [{"id": "a", "status": "idle|busy", "tasks_done": int}],
        "tokens_used": int,
        "uptime_s":    float,
    }
    """
    state = data.get("state", "running")
    level = {
        "running": "info",
        "completed": "success",
        "failed": "error",
        "stuck": "warning",
    }.get(state, "info")
    elements: list[dict[str, Any]] = [
        _field("State", state),
        _field("Uptime", f"{data.get('uptime_s', 0):.1f}s"),
        _field("Tokens", f"{data.get('tokens_used', 0):,}"),
    ]
    for a in data.get("agents", [])[:8]:  # 最多 8 个
        elements.append(
            _div(
                f"{'🟢' if a.get('status') == 'idle' else '🔵'} "
                f"`{a.get('id', '?')}` — done: {a.get('tasks_done', 0)}"
            )
        )
    if len(data.get("agents", [])) > 8:
        elements.append(_div(f"... and {len(data['agents']) - 8} more agents"))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(data.get("title", "Swarm Status"), level),
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# 5) confirm_dialog
# ---------------------------------------------------------------------------


def render_confirm_dialog(data: dict[str, Any]) -> dict[str, Any]:
    """
    确认对话框（Human-in-the-loop 审批）

    @param data {
        "title":   "Confirm Action",
        "message": "Are you sure?",
        "actions": [{"text": "Approve", "value": "approve", "type": "primary"},
                    {"text": "Deny",    "value": "deny",    "type": "danger"}],
    }
    """
    elements: list[dict[str, Any]] = [
        _div(data.get("message", "Please confirm")),
    ]
    actions = data.get(
        "actions",
        [
            {"text": "Approve", "value": "approve", "type": "primary"},
            {"text": "Deny", "value": "deny", "type": "danger"},
        ],
    )
    if actions:
        elements.append(_actions(actions))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(data.get("title", "Confirm"), "warning"),
        "elements": elements,
    }


# 模板注册表
TEMPLATES: dict[str, Any] = {
    "task_progress": render_task_progress,
    "code_review_result": render_code_review_result,
    "adversarial_debug": render_adversarial_debug,
    "swarm_status": render_swarm_status,
    "confirm_dialog": render_confirm_dialog,
}


def render_card(template: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    按模板名渲染卡片

    @param template  模板名（必须在 TEMPLATES 中）
    @param data      模板数据
    @return 完整卡片 dict
    @raise ValueError 未知模板
    """
    if template not in TEMPLATES:
        raise ValueError(f"unknown card template: {template!r}; valid: {sorted(TEMPLATES.keys())}")
    return TEMPLATES[template](data)


__all__ = [
    "TEMPLATES",
    "render_adversarial_debug",
    "render_card",
    "render_code_review_result",
    "render_confirm_dialog",
    "render_swarm_status",
    "render_task_progress",
]
