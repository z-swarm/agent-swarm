# agent-swarm Recipes（常见任务可复制粘贴的 YAML）

> DESIGN §17.7 配套。≥5 个常见任务，每个含可独立运行的 swarm 配置 + 预期行为。

## 1. PR 安全审查（pr_review.md）

**任务**：审查 PR diff 中的安全问题。

```yaml
# recipes/pr_review.yaml
name: pr-review
agents:
  - id: reviewer
    role: code-security-reviewer
    role_type: plan_only
    persona: |
      You are a security-focused code reviewer. Look for: SQL injection,
      command injection, hardcoded credentials, path traversal, weak crypto,
      and unsafe deserialization. Output findings with file:line and severity.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 5
tasks:
  - id: review-pr
    title: Review the staged PR diff for security issues
    description: |
      Run git diff main..HEAD to get the PR changes. Identify security issues
      with concrete file:line references. Output a structured report.
```

**跑法**：
```bash
git checkout feature/my-branch
agent-swarm run docs/recipes/pr_review.yaml
```

---

## 2. 生产调试（debug_production.md）

**任务**：根据堆栈/日志定位根因。

```yaml
# recipes/debug_production.yaml
name: debug-prod-incident
agents:
  - id: sre-lead
    role: senior SRE
    role_type: lead
    persona: |
      You are a senior SRE with 10 years of on-call experience. When debugging
      production incidents, you: 1) check SLO dashboards first, 2) read recent
      deploys, 3) check DB connection pool, 4) look for resource saturation,
      5) form hypotheses and gather evidence. Always state your confidence.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 5
  - id: db-specialist
    role: database specialist
    role_type: plan_only
    persona: |
      You are a PostgreSQL expert. You suspect N+1 queries, missing indexes,
      long-running transactions, and lock contention first.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 3
mcp_servers:
  log-search:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/var/log"]
tasks:
  - id: form-hypotheses
    title: Form hypotheses about root cause
    description: |
      Read the production stack trace and error logs. Form 3-5 hypotheses
      about the root cause, with evidence for each.
    assigned_to: sre-lead
  - id: verify-hypotheses
    title: Verify hypotheses via log search
    description: |
      Use the log-search MCP tool to find evidence for each hypothesis.
      Mark SUPPORT/REFUTE/UNCERTAIN with confidence and evidence.
    assigned_to: db-specialist
```

---

## 3. 文档生成（docs_generation.md）

**任务**：从代码自动生成 API 文档。

```yaml
# recipes/docs_generation.yaml
name: api-docs-generation
agents:
  - id: doc-writer
    role: technical writer
    role_type: plan_only
    persona: |
      You write clear API documentation. For each public function, document:
      purpose, parameters, return value, exceptions, and a usage example.
      Use Google-style docstring format.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 3
tasks:
  - id: scan-codebase
    title: List all public functions in src/
    description: |
      Scan src/agent_swarm/**/*.py and list every public function/class
      with its signature and one-line description.
  - id: generate-docs
    title: Generate API documentation
    description: |
      For each function from the scan, write a Google-style docstring
      with: Args, Returns, Raises, Example. Concatenate into a single
      docs/api.md file.
```

---

## 4. 数据库迁移（migration.md）

**任务**：评估 + 生成 Alembic/Prisma 迁移脚本。

```yaml
# recipes/migration.yaml
name: db-migration-plan
agents:
  - id: migration-planner
    role: database migration specialist
    role_type: plan_only
    persona: |
      You are a database migration expert. For every schema change, you:
      1) assess risk (downtime, data loss, lock duration), 2) check if
      backfill is needed, 3) write a forward+rollback plan, 4) flag any
      breaking changes for the API layer.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 3
tasks:
  - id: assess-change
    title: Assess the proposed schema change
    description: |
      Given the proposed schema change, output: risk_level, downtime_estimate,
      backfill_sql (if needed), forward_migration, rollback_migration,
      and api_breaking_changes.
```

---

## 5. 安全审计（security_audit.md）

**任务**：跑完整安全审计（PR 审查 + 攻击套件）。

```yaml
# recipes/security_audit.yaml
name: full-security-audit
agents:
  - id: auditor
    role: security auditor
    role_type: plan_only
    persona: |
      You are a security auditor. Check for: OWASP Top 10, secrets in code,
      unsafe dependencies, weak authentication, missing rate limits,
      improper error handling that leaks info, and compliance gaps (GDPR,
      SOC2). Output findings with severity and remediation.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 5
  - id: attacker
    role: red team
    role_type: plan_only
    persona: |
      You think like an attacker. For each finding, devise a concrete
      exploit scenario with PoC code. Mark which findings are exploitable
      in practice vs theoretical.
    provider: openai
    model: gpt-4o-mini
    max_iterations: 5
tasks:
  - id: scan-owasp
    title: Scan codebase for OWASP Top 10
    description: |
      Run a static scan for: injection, broken auth, sensitive data exposure,
      XXE, broken access control, misconfig, XSS, insecure deserialization,
      vulnerable components, insufficient logging.
    assigned_to: auditor
  - id: adversarial-verify
    title: AdversarialVerify exploitability
    description: |
      Run the auditor's findings through red-team review. For each finding,
      mark EXPLOITABLE / THEORETICAL / FALSE_POSITIVE with confidence.
    assigned_to: attacker
```

---

## 附录：所有 recipe 跑法汇总

```bash
agent-swarm run docs/recipes/pr_review.yaml
agent-swarm run docs/recipes/debug_production.yaml
agent-swarm run docs/recipes/docs_generation.yaml
agent-swarm run docs/recipes/migration.yaml
agent-swarm run docs/recipes/security_audit.yaml
```

**自定义**：复制任一 YAML，改 name/agents/tasks 即可。所有 recipe 共享相同的 agent_swarm 协议（DelegateMode/AdversarialVerifier）。
