# agent-swarm 🦞

独立的通用多 Agent 协作框架。Any OS. Any Platform. The lobster way.

## 核心理念

- **去中心化协调**: Task Queue + 乐观锁 CAS，agent 自己认领任务
- **点对点通信**: Mailbox 实现 agent 间直通，Team Lead 只编排不动手
- **对抗式验证**: 多 agent 互相质疑，在交叉验证中逼出真相
- **委托模式**: 协调者与执行者分离
- **多 Provider**: 不绑定单一 LLM，支持 OpenAI / Anthropic / DeepSeek / Ollama

## Quickstart

```bash
# 安装
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 设置 API key (任选其一)
export OPENAI_API_KEY=sk-...          # OpenAI: 环境变量
export ANTHROPIC_API_KEY=sk-ant-...   # Anthropic: 环境变量
# agent-swarm run --provider openai --api-key sk-...    # 方式 2: CLI flag (高于 env)
# agent-swarm run --provider anthropic --api-key sk-ant-...

# W1：单 agent 读 README 并总结
agent-swarm run examples/w1_hello.yaml

# W2：双 agent 协作
echo "payload-from-w2" > examples/data.txt
agent-swarm run examples/w2_two_agents.yaml

# W3：跑完查看 session 历史 + 恢复状态
agent-swarm run examples/w3_resume.yaml
agent-swarm session list
agent-swarm session show <session-id>
agent-swarm session resume <session-id>

# W4：跑 Golden Case G-001（PR 安全审查）
pytest tests/e2e/test_w4_golden_g001.py -v

# W5：SecurityPolicy + 攻击拦截
agent-swarm run examples/w5_secure.yaml
pytest tests/security/test_attack_suite.py -v

# W6：TUI 仪表盘（4 面板实时观测）
agent-swarm tui examples/w6_tui.yaml
# ↑ 退出按 q
```

# W7：Lead + Worker Delegate（Phase 2 第一个 Weekly Slice）
```bash
agent-swarm run examples/w7_delegate.yaml
# 预期: 退出码 0；ProtocolResult.success=True
```

# W7 程序化入口（自驱 agent / Phase 2+ 协议接入用）
```python
import asyncio
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.protocols import DelegateMode

async def main():
    swarm = Swarm.from_yaml("examples/w7_delegate.yaml")
    swarm.set_protocol(DelegateMode())
    result = await swarm.run_with_protocol()
    print(result.summary)

asyncio.run(main())
```

# W8：Adversarial Verify（Phase 2 第二个 Weekly Slice）
```bash
agent-swarm run examples/w8_adversarial.yaml
```
@note W8 骨架：judge_fn 默认抛 NotImplementedError；调用方需注入。
      完整 P2 Golden Case 根因定位见 `tests/golden/test_golden_p2.py`。

# W8 程序化入口
```python
import asyncio
from agent_swarm.core.adversarial import AdversarialVerifier
from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import Judgement, Stance

async def judge_fn(agent, hyp_id, round_no):
    # 这里接入真 LLM；示例给确定性脚本
    return Judgement(agent.id, hyp_id, round_no, Stance.SUPPORT, 0.9)

async def main():
    swarm = Swarm.from_yaml("examples/w8_adversarial.yaml")
    verifier = AdversarialVerifier(min_survivors=1, max_rounds=3)
    verdict = await verifier.verify(
        [t.title for t in swarm.tasks],
        list(swarm.agents),
        judge_fn=judge_fn,
    )
    print(f"root_cause: {verdict.root_cause}, reason: {verdict.convergence_reason}")

asyncio.run(main())
```

# W9：MCP 集成（Phase 2 第三个 Weekly Slice）
```bash
# 前置：Node.js + npx（filesystem / GitHub server 通过 npx 拉取）
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
agent-swarm run examples/w9_mcp_github_filesystem.yaml
```

# W9 程序化入口
```python
import asyncio
from agent_swarm.mcp import MCPRegistry, StdioMCPClient, await_build_tool_adapters
from agent_swarm.mcp.registry import MCPServerConfig

async def main():
    registry = MCPRegistry.from_dict({
        "filesystem": {"transport": "stdio", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        "github": {"transport": "stdio", "command": ["npx", "-y", "@modelcontextprotocol/server-github"]},
    })
    cfg = registry.get("filesystem")
    client = StdioMCPClient(cfg, timeout_s=10.0)
    adapters = await await_build_tool_adapters("filesystem", cfg, client)
    # 调 MCP 工具
    list_dir = next(a for a in adapters if a.mcp_tool_name == "list_directory")
    out = await list_dir.invoke({"path": "/tmp"})
    print(out)
    await client.disconnect()

asyncio.run(main())
```
@note W9 骨架：SSE 传输 / 重连熔断 推迟到 W10+（DESIGN §7.3 提到但
      Phase 2 DoD ③ 字面只要求"≥2 server"——stdio 已够）。

预期输出：CLI 打印任务结果表格 + agent 给出的一句话摘要。
W6 TUI 显示 4 面板：Status / Tasks / Messages / Token Budget。

> @note examples 数量: 当前 6 个 (w1/w2/w3/w5/w6/w7)。DESIGN §17.2 Phase 1 DoD ⑤ 写
> "3 个 examples"——Phase 1 完结时数量已超最低要求, §17.2 文字未同步
> (Phase 2+ 文档校对时一并改)。

## 审批流程（ApprovalFlow，DESIGN §8.3）

> **状态**: P2-3.4 落地（脚本模式）。飞书/邮件卡片等 ChannelAdapter 留待 Phase 2 W6+。

任何 `SecurityPolicy.check_tool()` 返回 `REQUIRE_APPROVAL` 的工具调用都必须经过 `ApprovalFlow.request_approval(decision, ctx)` 链——任一 approver 返回 `True` 即放行。

**默认行为（fail-closed）**：
- 未注入 approver → **拒绝** + audit log（`approval.denied tenant=... session=... reason=...`）
- 适合生产环境默认安全姿态

**脚本模式（auto-grant / 自动化）**：
```python
from agent_swarm.security import ApprovalFlow, SecurityContext

flow = ApprovalFlow()
flow.append_approver(lambda decision, ctx: True)  # auto-allow-all
# 或基于 decision 条件放行：
flow.append_approver(lambda d, c: d.reason.startswith("whitelist:"))
```

**使用入口**：
- `RunCommandTool(policy, sandbox, approval_flow=flow)` — 注入到 `run_command` 工具
- 未来 ChannelAdapter 接入：`flow.append_approver(feishu_card_approver(timeout=300))`

**端到端 e2e**：`tests/e2e/test_w10_approval_e2e.py`（11 个场景）。

## Git Blame Ignore (W38)

本项目在历史上有大规模格式化 commit (W36e 1 原子 commit ruff format 150 文件), 这些 commit 会污染 `git blame`, 每行都显示该 commit 为最后修改者, 影响代码溯源。

为此, 仓库根目录有 `.git-blame-ignore-revs` 文件, 记录需要跳过的大规模 commit。

**启用方法 (per-repo, 不放全局配置):**

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

**验证效果:**

```bash
# 不启用时: blame W36e 的 commit hash
git log --oneline -1 -- README.md

# 启用后: blame 跳过 W36e, 回到上一次实质修改
git blame README.md | head -5
```

详见 `.git-blame-ignore-revs` 文件内嵌注释。

## 开发

```bash
# 全套测试 (501 项, < 30 秒)
pytest tests/unit/ tests/e2e/ tests/golden/ tests/security/

# 覆盖率 (Phase 1 门槛 75%, 当前 93.36%)
pytest --cov=src/agent_swarm --cov-report=term-missing

# Lint + 类型
ruff check src/ tests/ tools/
mypy src/

# 性能基线 (DESIGN §17.5)
python tools/benchmark.py --cases tests/golden/cases --baseline tests/golden/baseline.yaml
```

## 跨平台支持（Windows / WSL / Linux / macOS）

> **P2-3.5 落地**：审计报告指出 `.venv/bin/` 是 Linux 路径 + WSL pytest cache 权限问题。
> 实际跑通需按平台选择虚拟环境激活脚本。

### Linux / macOS / WSL

```bash
python3 -m venv .venv
source .venv/bin/activate          # bash/zsh
pip install -e ".[dev]"
agent-swarm run examples/w1_hello.yaml
```

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
agent-swarm run examples\w1_hello.yaml
```

### Windows (cmd.exe)

```cmd
py -3.11 -m venv .venv
.\.venv\Scripts\activate.bat
pip install -e ".[dev]"
```

### WSL 下的两个常见坑

1. **pytest cache 写权限**：从 Windows 浏览器打开 WSL 项目，`.pytest_cache/v/cache/nodeids` 可能写不进去
   → **解法**：始终在 WSL 内跑测试（`wsl` 进 shell 再 `pytest`），不要从 Windows 端跨边界

2. **.ruff_cache 同样问题**：`ruff check` 也会写 cache 到 `.ruff_cache/`
   → **解法**：设环境变量 `RUFF_CACHE_DIR=/tmp/ruff-cache` 写到 /tmp；或同步用 `wsl` 跑

### .gitignore 已覆盖的跨平台产物

```
.pytest_cache/     .coverage  .coverage.*  .mypy_cache/  .ruff_cache/
__pycache__/       *.py[cod]  *.egg-info/  *.egg
```

完整 `.gitignore` 见项目根目录。所有 cache / 覆盖数据 / DB 都被排除，**不会污染 Windows + WSL 双向同步的 git status**。

## 项目结构（Phase 1 收尾）

```
agent-swarm-g1/
├── DESIGN.md                       # 架构设计 v4.2
├── pyproject.toml                  # 依赖 + ruff + mypy + pytest 配置
├── tools/benchmark.py              # §17.5 性能基线
├── src/agent_swarm/
│   ├── core/                       # Agent / Task / Swarm / Mailbox / TokenBudget
│   ├── providers/                  # OpenAI / Anthropic Provider
│   ├── tools/builtin/              # read_file / run_command / send_message
│   ├── security/                   # SecurityContext / Policy / Sandbox / ApprovalFlow
│   ├── observability/              # Bus / JsonLog / InMemory / Sqlite (含 tenant_id)
│   ├── mcp/                        # MCP 工具集成 (Phase 2 W9)
│   ├── skills/                     # Skill 系统
│   ├── golden.py                   # Golden Case runner
│   ├── tui/                        # Textual 4 面板仪表盘
│   └── cli/                        # agent-swarm CLI (run/session/tui, --api-key)
├── tests/
│   ├── unit/                       # 单测
│   ├── e2e/                        # Weekly Slice DoD
│   ├── security/                   # 攻击套件 (25 条)
│   ├── golden/                     # P1 Golden Case (G-001..G-010) + baseline.yaml
│   └── conftest.py                 # FakeLLMProvider 脚本回放
└── examples/
    ├── w1_hello.yaml               # W1
    ├── w2_two_agents.yaml          # W2
    ├── w3_resume.yaml              # W3
    ├── w5_secure.yaml              # W5
    ├── w6_tui.yaml                 # W6
    ├── w7_delegate.yaml            # W7 (Phase 2 Delegate Mode)
    ├── w8_adversarial.yaml          # W8 (Phase 2 Adversarial Verify)
    └── w9_mcp_github_filesystem.yaml # W9 (Phase 2 MCP)
```

后续 Weekly Slice 会持续扩展（参见 [DESIGN.md §15](./DESIGN.md#15-mvp-分阶段计划)）。
