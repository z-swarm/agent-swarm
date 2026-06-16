"""
@module agent_swarm.skills.review
@brief  内置技能：code-review:security 等审查类（W4）

DESIGN.md §11.2 内置技能库。W4 落地：
  - code-review:security  (W4 必须，Golden Case G-001 用)
  - code-review:performance  (占位，prompt 已就绪，留待 W6+ 验证)
  - code-review:architecture (占位)

设计要点：
  - prompt extension 描述清晰的检查清单 → 提升 LLM 输出可预测性
  - 输出格式约定：每条发现含 file:line / category / severity / description
  - 不带特殊工具——复用 read_file / search_code（agent yaml 中显式授权）
"""

from __future__ import annotations

from agent_swarm.skills.base import Skill, SkillRegistry

# ---------------------------------------------------------------------------
# code-review:security
# ---------------------------------------------------------------------------


_SECURITY_PROMPT = """
You are reviewing code for security vulnerabilities. Focus areas:

1. SQL Injection
   - Look for string concatenation / f-strings building SQL
   - Flag any raw user input reaching execute() / query() without parameterization
   - Pattern: "SELECT ... " + var, f"... {var} ...", %s formatting

2. Cross-Site Scripting (XSS)
   - Look for user input rendered without escaping (innerHTML, dangerouslySetInnerHTML)
   - Flag template engines used without auto-escape

3. Authentication & Authorization
   - Missing auth checks before sensitive operations
   - Hardcoded credentials / API keys
   - Weak password handling (plain text, MD5, SHA1)

4. Path Traversal
   - User-controlled paths in open() / read_file() without normalization
   - Flag any "../" reaching filesystem APIs

5. Command Injection
   - subprocess / os.system with user-controlled args
   - shell=True with concatenated strings

6. Sensitive Data Exposure
   - Logging passwords / tokens / PII
   - Error messages leaking stack traces to users

When you finish reviewing, output findings in this exact format (one per line):
  - [<severity>] <file>:<line> <category>: <description>

Severity: CRITICAL / HIGH / MEDIUM / LOW
Category: SQL_INJECTION / XSS / AUTH / PATH_TRAVERSAL / CMD_INJECTION / DATA_EXPOSURE / OTHER

If no issues found, output exactly:
  No security issues found.

Be specific: cite the exact line, not "somewhere in the file".
""".strip()


def _register_builtin_skills() -> None:
    """启动时注册内置技能——幂等：重复 import 不抛"""
    if SkillRegistry.get("code-review:security") is None:
        SkillRegistry.register(
            Skill(
                id="code-review:security",
                description=(
                    "Detect SQL injection, XSS, authentication issues, "
                    "path traversal, command injection and sensitive data "
                    "exposure in source code."
                ),
                version="1.0",
                category="review",
                system_prompt_extension=_SECURITY_PROMPT,
                required_tools=["read_file"],
                metadata={
                    "checks": [
                        "SQL_INJECTION", "XSS", "AUTH",
                        "PATH_TRAVERSAL", "CMD_INJECTION", "DATA_EXPOSURE",
                    ],
                },
            )
        )

    if SkillRegistry.get("code-review:performance") is None:
        SkillRegistry.register(
            Skill(
                id="code-review:performance",
                description=(
                    "Detect N+1 queries, memory leaks, algorithmic "
                    "complexity issues, and inefficient I/O patterns."
                ),
                version="1.0",
                category="review",
                system_prompt_extension=(
                    "Focus on: (1) N+1 queries (SELECT inside loops); "
                    "(2) unbounded memory growth (lists growing without limit); "
                    "(3) O(n²) or worse algorithms on user-scale data; "
                    "(4) blocking I/O in async contexts."
                ),
                required_tools=["read_file"],
            )
        )

    if SkillRegistry.get("code-review:architecture") is None:
        SkillRegistry.register(
            Skill(
                id="code-review:architecture",
                description=(
                    "Review module coupling, SOLID principles, and "
                    "design pattern fit."
                ),
                version="1.0",
                category="review",
                system_prompt_extension=(
                    "Focus on: cohesion / coupling, single responsibility, "
                    "open-closed, dependency direction, abstraction boundaries."
                ),
                required_tools=["read_file"],
            )
        )


# 模块级 import 时自动注册——AgentRunner 解析 skills: [...] 时已就绪
_register_builtin_skills()
