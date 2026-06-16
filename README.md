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

🟢 **Phase 1 / Week 4 完成** — KnowledgeBase + Skill 系统 + Golden Case G-001

| 周 | 切片 | 状态 |
|---|------|-----|
| W1 | 单 agent + CLI hello | ✅ |
| W2 | 双 agent + Mailbox + TaskQueue + Anthropic | ✅ |
| W3 | SQLite 持久化 + Session 恢复 + Observability | ✅ |
| W4 | KB + Skill + Golden Case G-001 | ✅ |
| W5 | SecurityContext + Sandbox | ⬜ |
| W6 | TUI + Phase 1 DoD 全过 | ⬜ |

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
```

预期输出：CLI 打印任务结果表格 + agent 给出的一句话摘要。

## 开发

```bash
# 全套测试 (29 项, < 1 秒)
pytest tests/unit/ tests/e2e/

# 覆盖率（W1 门槛 75%）
pytest --cov=src/agent_swarm --cov-report=term-missing

# Lint + 类型
ruff check src/ tests/
mypy src/
```

## 项目结构（W1）

```
agent-swarm-g1/
├── DESIGN.md                       # 架构设计 v4.2
├── pyproject.toml                  # 依赖 + ruff + mypy + pytest 配置
├── src/agent_swarm/
│   ├── core/                       # Agent / Task / Swarm
│   ├── providers/                  # OpenAI Provider
│   ├── tools/builtin/              # read_file
│   └── cli/                        # agent-swarm CLI
├── tests/
│   ├── unit/                       # 26 个单元测试
│   ├── e2e/                        # 3 个 W1 e2e
│   └── conftest.py                 # FakeLLMProvider 脚本回放
└── examples/
    └── w1_hello.yaml               # W1 演示配置
```

后续 Weekly Slice 会持续扩展（参见 [DESIGN.md §15](./DESIGN.md#15-mvp-分阶段计划)）。
