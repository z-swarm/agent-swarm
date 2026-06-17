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

| 周 | 切片 | 状态 | DoD |
|---|------|-----|-----|
| W1 | 单 agent + CLI hello | ✅ | 退出码 0 + 关键词 |
| W2 | 双 agent + Mailbox + TaskQueue + Anthropic | ✅ | CAS 冲突 ≥1 |
| W3 | SQLite 持久化 + Session 恢复 + Observability | ✅ | kill -9 状态 100% 重建 |
| W4 | KB + Skill + Golden Case G-001 | ✅ | G-001 通过 + KB 命中 ≥60% |
| W5 | SecurityContext + Sandbox + TokenBudget | ✅ | 25/25 攻击拦截 + 截断不崩溃 |
| W6 | TUI 仪表盘 (Textual) | ✅ | 5 秒内完整视图 |

## Quickstart

```bash
# 安装
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# W1：单 agent 读 README 并总结
export OPENAI_API_KEY=sk-...
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

预期输出：CLI 打印任务结果表格 + agent 给出的一句话摘要。
W6 TUI 显示 4 面板：Status / Tasks / Messages / Token Budget。

## 开发

```bash
# 全套测试 (453 项, < 30 秒)
pytest tests/unit/ tests/e2e/

# 覆盖率（Phase 1 门槛 75%, 当前 93.33%）
pytest --cov=src/agent_swarm --cov-report=term-missing

# Lint + 类型
ruff check src/ tests/
mypy src/
```

## 项目结构（Phase 1 收尾）

```
agent-swarm-g1/
├── DESIGN.md                       # 架构设计 v4.2
├── pyproject.toml                  # 依赖 + ruff + mypy + pytest 配置
├── src/agent_swarm/
│   ├── core/                       # Agent / Task / Swarm / Mailbox / TokenBudget
│   ├── providers/                  # OpenAI / Anthropic Provider
│   ├── tools/builtin/              # read_file / run_command / send_message
│   ├── security/                   # SecurityContext / Policy / Sandbox
│   ├── observability/              # Bus / JsonLog / InMemory / Sqlite
│   ├── skills/                     # Skill 系统
│   ├── golden.py                   # Golden Case runner
│   ├── tui/                        # Textual 4 面板仪表盘
│   └── cli/                        # agent-swarm CLI (run/session/tui)
├── tests/
│   ├── unit/                       # 单测
│   ├── e2e/                        # Weekly Slice DoD
│   ├── security/                   # 攻击套件
│   └── conftest.py                 # FakeLLMProvider 脚本回放
└── examples/
    ├── w1_hello.yaml               # W1
    ├── w2_two_agents.yaml          # W2
    ├── w3_resume.yaml              # W3
    ├── w5_secure.yaml              # W5
    └── w6_tui.yaml                 # W6
```

后续 Weekly Slice 会持续扩展（参见 [DESIGN.md §15](./DESIGN.md#15-mvp-分阶段计划)）。
