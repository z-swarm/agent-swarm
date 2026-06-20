# W14a 演示录屏 placeholder

> ⚠️ 本文件是录屏占位——实际录屏文件（`wk14a-mcp-resilience.mp4`）需在带 GUI/TTY 的环境手工录制

## 录屏内容（≤120 秒）

按下面脚本演示 `tools/count_reconnect.py` 输出 + 跑测试：

```bash
# 1. 跑测试看 CircuitBreaker 状态机（30s）
python -m pytest tests/unit/test_mcp_circuit_breaker.py -v --tb=short

# 2. 跑 count_reconnect 验证脚本（30s）
python tools/count_reconnect.py

# 3. 跑 G-018 Golden Case（30s）
python -m pytest tests/golden/test_golden_p2.py -v -k g018
```

## 录屏要求（DESIGN §17.1）

- 文件名: `demos/wk14a-mcp-resilience.mp4`
- 时长: ≤ 120 秒
- 内容: CircuitBreaker 状态转移 + 重连日志 + 熔断后快速拒绝

## 手工录制步骤（Linux/macOS with ffmpeg）

```bash
# 需要 asciinema 录终端或 ffmpeg 录桌面
asciinema rec -c "python tools/count_reconnect.py" demos/wk14a-mcp-resilience.cast
# 然后用 agg 或其他工具转 mp4（≤120s）
```

## Windows 录屏

```powershell
# Windows Game Bar (Win+G) 或 ffmpeg
ffmpeg -f gdigrab -framerate 30 -i desktop -t 120 demos/wk14a-mcp-resilience.mp4
```

## 校验（§17.1 三件套）

```bash
test -f demos/wk14a-mcp-resilience.mp4 && \
ffprobe -show_entries format=duration demos/wk14a-mcp-resilience.mp4 2>/dev/null | \
  awk '/duration/{ exit ($2>120) }' && \
echo "§17.1 三件套: ✓ 视频存在 + 时长 ≤ 120s"
```

## 当前状态

- 本仓库 demos/ 目录已创建
- 实际录屏文件 `wk14a-mcp-resilience.mp4` 需用户手工录制
- W14a Demo Tag `w14a-demo` 待录制完成后打

@note W14a 强制规则：每个 DoD 通过后必须 git tag w14a-demo + 录屏
@note 录屏在 W14b 之前可作为"周中临时产出"先空着；周末前补齐即可
