# Prompt Cache 实施计划

Spec：[设计规范](../specs/2026-11-22-prompt-cache-design.md)。
基线：`7665e8684f5a124fe789f21909ee54325edc556b`。
执行：当前会话逐项实施；最后由独立第二读者复审全分支。

## Task 1：adapter 策略与稳定 wire 前缀

- 在 `tests/test_prompt_cache.py` 先写红测：真实 manifest、三协议开关、
  JSON/SSE、工具排序、输入不可变、动态尾部和 opaque 保真。
- 增加纯标准库 `src/tianshu/prompt_cache.py`，接到三个 adapter 构造及请求点。
- 不改内部消息、C1–C5 或事件版本；不改授权、重试、保留策略或预算。
- 运行定向测试，记录红/绿证据。

## Task 2：原生缓存用量解析与落库回归

- 先写 Chat 原生 JSON/SSE usage 红测及真实主循环持久化/重放测试。
- 补齐原生嵌套读取，保留 None 与快照语义，覆盖非法值和矛盾值。
- 运行 model/Responses/usage/pricing 受影响域回归；不实现未经核实的缓存计价。

## Task 3：缓存与质量旁路报告

- 先写 provider 分母、缺失值、重复事件、未决 attempt、混合上下文、失败质量红测。
- 新增纯统计模块；`evaluate --cache-report PATH` 复用已有真实 observations 和判据。
- 报告显式区分离线/线上证据，不额外发模型请求；不改 C5 报告及既有可比性。

## Task 4：冻结、验证、知识库对账

- 更新使用说明、知识库边界及独立 changelog 碎片。
- 所有修改完成后运行 `uv lock --check`、`uv run --frozen ruff check .`、
  `git diff --check` 和本机完整 `uv run --frozen pytest`。
- 独立第二读者核完整 diff、亲跑探针，结论绑定 SHA/code tree。
- 修复确认阻断后重绑适用验证；真实付费缓存/质量实验维持未执行。
- 候选冻结后统一运行候选 CI，不使用旧批次 PASS 或反复推送跑矩阵。
