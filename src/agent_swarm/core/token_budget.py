"""
@module agent_swarm.core.token_budget
@brief  TokenBudgetManager——DESIGN.md §9.3（W5 简化版）

W5 范围:
  - calculate_usage: 估算 messages + tool schemas 的 token 总量
  - limit_tool_result: 大结果截断
  - smart_truncate: 消息列表截断（保留 system + 最近 N + 引用）

W6+ 范围（不在 W5 实现）:
  - generate_summary: 实际调 LLM 生成滑动窗口摘要
  - 跨多模型差异化 token 计数（tiktoken vs sentencepiece）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TokenBudget:
    """Token 用量快照"""

    used_tokens: int
    limit_tokens: int
    reserve_tokens: int
    remaining_tokens: int
    needs_truncation: bool


class TokenBudgetManager:
    """
    主动 token 预算管理——W5 简化

    @note W5 用粗估 1 token ≈ 4 chars（保守估计）
          W6+ 替换为 tiktoken 或 provider 实际计数
    """

    # 粗估：1 token ≈ 4 chars（适用于英文 + 代码；中文/特殊 token 可能偏差较大）
    _CHARS_PER_TOKEN = 4

    def __init__(
        self,
        context_window: int,
        reserve_tokens: int = 4096,
        warning_threshold: float = 0.8,
    ) -> None:
        """
        @param context_window    模型上下文窗口大小（如 gpt-4o-mini=128000）
        @param reserve_tokens    预留给模型回复的 token
        @param warning_threshold 触发截断的阈值（默认 80%）
        """
        self.context_window = context_window
        self.reserve_tokens = reserve_tokens
        self.warning_threshold = warning_threshold

    # ------------------------------------------------------------------
    # 计数
    # ------------------------------------------------------------------
    def _estimate_chars(self, text: str) -> int:
        """字符数粗估 token——utf-8 编码后再除以 4"""
        if not text:
            return 0
        return len(text.encode("utf-8")) // self._CHARS_PER_TOKEN

    def calculate_usage(
        self,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict] | None = None,
    ) -> TokenBudget:
        """
        估算当前对话的 token 总量

        @param messages    OpenAI/Anthropic 格式的 messages 列表
                          每条含 role + content（+ tool_calls/tool_use）
        @param tool_schemas 工具 schema 列表——也会消耗 token
        """
        used = self._estimate_chars(system_prompt)
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                # Anthropic 风格的 content blocks——逐 block 估算
                for block in content:
                    if isinstance(block, dict):
                        used += self._estimate_chars(str(block))
            else:
                # 字符串/None 路径
                used += self._estimate_chars(content or "")
            # tool_calls / tool_use 也会消耗
            for tc in m.get("tool_calls") or []:
                used += self._estimate_chars(str(tc))
        # tool schemas
        if tool_schemas:
            used += self._estimate_chars(str(tool_schemas))
        used += self.reserve_tokens

        remaining = max(0, self.context_window - used)
        needs_trunc = used > self.context_window * self.warning_threshold
        return TokenBudget(
            used_tokens=used,
            limit_tokens=self.context_window,
            reserve_tokens=self.reserve_tokens,
            remaining_tokens=remaining,
            needs_truncation=needs_trunc,
        )

    # ------------------------------------------------------------------
    # 截断
    # ------------------------------------------------------------------
    def smart_truncate(
        self,
        messages: list[dict],
        budget: TokenBudget,
        keep_recent: int = 6,
    ) -> list[dict]:
        """
        智能截断——保留 system + 最近 N 条 + 必要引用

        W5 简化策略（无 summary）：
          1. 保留 system turn
          2. 保留最近 keep_recent 条 turn
          3. 中间部分丢弃（一次性）
          4. W6+ 替换为 generate_summary
        """
        if not messages or budget.remaining_tokens > 0:
            return messages

        # 1) 找 system turn
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        # 2) 保留最近 N
        keep = other_msgs[-keep_recent:] if len(other_msgs) > keep_recent else other_msgs
        truncated = system_msgs + keep
        dropped = len(messages) - len(truncated)
        if dropped > 0:
            log.warning(
                "token_budget.truncated dropped=%d kept=%d (no summary in W5)",
                dropped,
                len(truncated),
            )
        return truncated

    def limit_tool_result(self, result: str, max_chars: int = 10000) -> str:
        """
        工具返回内容限长——超 max_chars 截断并加标记

        W5 默认:
          - read_file: 10000 chars
          - run_command: 10240 bytes (DESIGN §9.3)

        @note 截断后写临时文件 + 摘要——W6+ 实现
        """
        if len(result) <= max_chars:
            return result
        truncated = result[:max_chars]
        return f"{truncated}\n[truncated {len(result) - max_chars} chars]"
