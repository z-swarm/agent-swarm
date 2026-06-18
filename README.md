# agent-swarm 🦞

独立的通用多 Agent 协作框架。Any OS. Any Platform. The lobster way.

> 架构设计文档: [DESIGN.md](./DESIGN.md)

## 核心理念

- **去中心化协调**: Task Queue + 乐观锁 CAS，agent 自己认领任务
- **点对点通信**: Mailbox 实现 agent 间直通，Team Lead 只编排不动手
- **对抗式验证**: 多 agent 互相质疑，在交叉验证中逼出真相
- **委托模式**: 协调者与执行者分离
- **多 Provider**: 不绑定单一 LLM，支持 OpenAI / Anthropic / DeepSeek / Ollama

## 状态

🟢 **Phase 1 完成** — 6 周垂直切片全部 DoD 通过；TUI 仪表盘实时观测
🟡 **Phase 2 W7 (Delegate Mode) 完成** — Lead + Worker 分离端到端跑通

| 周 | 切片 | 状态 | DoD |
|---|------|-----|-----|
| W1 | 单 agent + CLI hello | ✅ | 退出码 0 + 关键词 |
| W2 | 双 agent + Mailbox + TaskQueue + Anthropic | ✅ | CAS 冲突 ≥1 |
| W3 | SQLite 持久化 + Session 恢复 + Observability | ✅ | kill -9 状态 100% 重建 |
| W4 | KB + Skill + Golden Case G-001 | ✅ | G-001 通过 + KB 命中 ≥60% |
| W5 | SecurityContext + Sandbox + TokenBudget | ✅ | 25/25 攻击拦截 + 截断不崩溃 |
| W6 | TUI 仪表盘 (Textual) | ✅ | 5 秒内完整视图 |
| **W7** | **Delegate Mode (Lead + Worker)** | ✅ | **1 lead + 2 workers 协作；lead 工具权限拦截；ProtocolResult 含 lead/worker 分组** |

## Quickstart

```bash
# 安装
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 设置 API key (任选其一)
export OPENAI_API_KEY=sk-...          # 方式 1: 环境变量
# agent-swarm run --api-key sk-...   # 方式 2: CLI flag (高于 env)

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

预期输出：CLI 打印任务结果表格 + agent 给出的一句话摘要。
W6 TUI 显示 4 面板：Status / Tasks / Messages / Token Budget。

> @note examples 数量: 当前 6 个 (w1/w2/w3/w5/w6/w7)。DESIGN §17.2 Phase 1 DoD ⑤ 写
> "3 个 examples"——Phase 1 完结时数量已超最低要求, §17.2 文字未同步
> (Phase 2+ 文档校对时一并改)。

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
    └── w7_delegate.yaml            # W7 (Phase 2 Delegate Mode)
```

后续 Weekly Slice 会持续扩展（参见 [DESIGN.md §15](./DESIGN.md#15-mvp-分阶段计划)）。
