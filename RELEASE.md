# RELEASE-0.5.0 — TestPyPI / PyPI 发布步骤

> 0.5.0 final release (W36g) 发布到 TestPyPI / PyPI 的完整步骤
> 准备时间: 2026-06-24 (W38)
> **范围**: dist 已构建 (`dist/agent_swarm-0.5.0*.{tar.gz,whl}`), `twine check` PASSED
> **阻塞**: 上传需用户环境 `~/.pypirc` token + non-interactive terminal

## 1. 前置条件

### 1.1 PyPI 账号

- 注册 [PyPI 账号](https://pypi.org/account/register/) (如未注册)
- 注册 [TestPyPI 账号](https://test.pypi.org/account/register/) (如未注册)
- 在两个账号下都创建 API token:
  - PyPI: https://pypi.org/manage/account/token/
  - TestPyPI: https://test.pypi.org/manage/account/token/
- Token scope 选 "Entire account" (或限定到 "agent-swarm" project, 如已存在)

### 1.2 本地 `~/.pypirc` 配置

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-AgEIcHlwaS5...<你的 PyPI token>

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgENdGVzdC5weXBp...<你的 TestPyPI token>
```

**注意**:
- `username = __token__` 是 PyPI 官方规定的字面值
- `password` 填完整的 token (含 `pypi-` 前缀)
- 文件权限: `chmod 600 ~/.pypirc`

### 1.3 工具检查

```bash
# 必须已装 (W36g 已构建, W36d 模式)
python -m build --version
twine --version
```

## 2. 发布到 TestPyPI (推荐先走)

### 2.1 验证 dist 完整

```bash
# 在项目根目录
ls -lh dist/agent_swarm-0.5.0*

# 期望:
# agent_swarm-0.5.0-py3-none-any.whl  (~240KB)
# agent_swarm-0.5.0.tar.gz            (~480KB)
```

### 2.2 twine check (已 PASSED, 复跑确认)

```bash
.venv/bin/twine check dist/agent_swarm-0.5.0*

# 期望输出: Checking dist/agent_swarm-0.5.0-py3-none-any.whl: PASSED
#           Checking dist/agent_swarm-0.5.0.tar.gz: PASSED
```

### 2.3 上传到 TestPyPI

```bash
# 关键: --repository testpypi, 不漏
.venv/bin/twine upload --repository testpypi dist/agent_swarm-0.5.0*
```

**预期输出**:
```
Uploading distributions to https://test.pypi.org/legacy/
Uploading agent_swarm-0.5.0-py3-none-any.whl
100% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 234.2/234.2 kB
Uploading agent_swarm-0.5.0.tar.gz
100% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 480.1/480.1 kB

View at:
https://test.pypi.org/project/agent-swarm/0.5.0/
```

### 2.4 TestPyPI 验证

1. 访问 https://test.pypi.org/project/agent-swarm/0.5.0/
2. 确认:
   - description 显示 "Phase 5: GUI Web UI + WebState 协议 + 真实 LLM 接入"
   - keywords 13 个
   - classifiers 9 个
   - homepage (如配置) 正确
3. 试装 (新 venv):
   ```bash
   python -m venv /tmp/test-venv
   /tmp/test-venv/bin/pip install --index-url https://test.pypi.org/simple/ agent-swarm==0.5.0
   /tmp/test-venv/bin/python -c "import agent_swarm; print(agent_swarm.__version__)"
   # 期望: 0.5.0
   ```

## 3. 发布到正式 PyPI (TestPyPI 验证后)

### 3.1 上传

```bash
# 关键: 不带 --repository, 默认走 PyPI
.venv/bin/twine upload dist/agent_swarm-0.5.0*
```

**预期输出**:
```
Uploading distributions to https://upload.pypi.org/legacy/
Uploading agent_swarm-0.5.0-py3-none-any.whl
100% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 234.2/234.2 kB
Uploading agent_swarm-0.5.0.tar.gz
100% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 480.1/480.1 kB

View at:
https://pypi.org/project/agent-swarm/0.5.0/
```

### 3.2 PyPI 验证

1. 访问 https://pypi.org/project/agent-swarm/0.5.0/
2. 试装 (新 venv):
   ```bash
   python -m venv /tmp/prod-venv
   /tmp/prod-venv/bin/pip install agent-swarm==0.5.0
   /tmp/prod-venv/bin/agent-swarm --help
   ```

### 3.3 创建 GitHub Release (可选但推荐)

```bash
# 在 GitHub 仓库页面 → Releases → Draft new release
# - Tag: 0.5.0 (选已存在的 tag)
# - Title: agent-swarm 0.5.0
# - Description: 复制 CHANGELOG.md 0.5.0 节点内容
# - 附件: 上传 dist/agent_swarm-0.5.0-py3-none-any.whl 和 .tar.gz
```

## 4. 发布后验证清单

- [ ] TestPyPI 页面 https://test.pypi.org/project/agent-swarm/0.5.0/ 显示正确
- [ ] TestPyPI 安装测试 (2.4 步骤) 通过
- [ ] PyPI 页面 https://pypi.org/project/agent-swarm/0.5.0/ 显示正确
- [ ] PyPI 安装测试 (3.2 步骤) 通过
- [ ] `pip install agent-swarm` (不带版本) 应能装到 0.5.0 (或更新)
- [ ] GitHub Release 创建 (含 dist 附件)
- [ ] docs/MEMORY.md 增 W38 段 "PyPI 上传完成" 状态
- [ ] CHANGELOG.md 0.5.0 节点加 "Published to PyPI" 标记

## 5. 已知限制 (W38 范围)

- **范围收口**: dist ready (`twine check` PASSED), 上传需用户环境
- **Token 安全**: `~/.pypirc` 不进 git, 用户自管
- **不可撤回**: PyPI 发布后版本固定, 错版本只能 yank + 新版本
- **多 factor**: 0.5.0 是 P5 收口, 后续 0.5.1 / 0.6.0 仍需走此流程

## 6. 失败处理

| 失败 | 处理 |
|------|------|
| Token 错 | 重新生成 token, 更新 `~/.pypirc` |
| 元数据错 | 改 pyproject.toml, 重新 `python -m build` + `twine check` |
| 误推到 PyPI | 立即 yank (`twine upload --yank`), 然后修版本号 (0.5.0 → 0.5.1) |
| 网络错 | 重试, twine 自带断点续传 |
| 权限错 | 确认 token scope 含 "agent-swarm" project |

## 7. 引用

- `pyproject.toml` — 当前 0.5.0, 13 keywords, 9 classifiers
- `CHANGELOG.md` 0.5.0 节点 — release 内容
- `dist/` — 0.5.0 sdist + wheel
- `git tag 0.5.0` (W36g) — release 标签
- `W36g_PLAN.md` — 0.5.0 release 节奏
- `W37_PLAN.md` — W37 真实 LLM 接入 (0.5.0 final 价值兑现)
- `W38_PLAN.md` — 本 slice 0.5.0 final production-ready 收口
