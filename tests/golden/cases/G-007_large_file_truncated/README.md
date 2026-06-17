# G-007_large_file_truncated

大文件 (10MB) 读取应截断不崩溃

@note Phase 1 DoD (DESIGN §17.2 W3) 占位 case——内容由对应 e2e 测试驱动:
  - pytest tests/e2e/test_w*.py::test_... (具体见 expected.yaml 注释)
