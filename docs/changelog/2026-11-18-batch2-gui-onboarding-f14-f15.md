# 2026-11-18 · 第 2/3 批：首日图形化设置闭环、`--desktop` 目录打通、F15 合规门禁与 D7/F14 默认脱敏诊断包

## 背景与解决的问题

针对项目盘点指出的「普通用户首日产品化盲区」与 G3 合规/可支持性缺口，本批次在不触碰 C1–C5 封闭集契约的前提下完成四块交付：

1. **首日图形化设置与控制面闭环（`src/tianshu/settings_control.py` + `ui/src/components/SettingsDialog.tsx`）**
   - 控制面新增 12 个 RPC：`settings.get`、`model.configure`、`credential.set`、`credential.delete`、`schedule.list`、`schedule.create`、`schedule.set_enabled`、`schedule.remove`、`budget.view`、`mcp.list`、`diagnostics.preview`、`diagnostics.export`。
   - `RuntimeModelSlot` 支持在不重启 Daemon 的前提下热切换模型协议（`scripted` / `openai` / `anthropic`）、模型名与 Base URL；非敏感配置以 `0o600` 原子写 `<data_root>/model_config.json`，拒绝未知参数键与凭据字段。
   - `credential.set / delete` 仅面向可写凭据后端（如 macOS Keychain），密钥写入前立即调用 `register_secret()` 纳入进程脱敏表，RPC 响应**零密钥回显**，环境变量只读后端与其他平台如实返回 `unsupported`。
   - React UI 顶栏新增「设置」入口，汇总「模型与密钥 / 定时任务 / 预算账本 / MCP 服务 / 诊断包导出」五个标签页。

2. **CLI 与桌面端默认数据目录打通（`src/tianshu/cli.py` / `src/tianshu/schedule_cli.py`）**
   - 新增 `--desktop` 与 `TIANSHU_USE_DESKTOP_DB=1`，指向 `ipc.default_data_root() / "events.db"`；与显式 `--db` 同时传入时快速报错，不静默择一。
   - 当 CWD 下 `.tianshu/events.db` 不存在且桌面端默认库已存在时，交互式 TTY 在 `stderr` 输出单行提示，不污染 `--json` 标准输出。

3. **F15 开源许可证合规校验与 CycloneDX 1.5 SBOM（`scripts/check_licenses_and_sbom.py` + `.github/workflows/ci.yml`）**
   - 纯 Python 标准库校验根 `LICENSE`（Apache-2.0）、`pyproject.toml` 零第三方运行时依赖（`dependencies = []`），对 `uv.lock` 全量包、`ui/package.json` 直接依赖及 `desktop/src-tauri/Cargo.toml` 全部依赖节（含 `workspace.dependencies` 与 `target.*.dependencies`）执行宽松许可白名单与 GPL/LGPL/AGPL/SSPL/BUSL/Proprietary 禁令检查，并从 `uv.lock`、`ui/pnpm-lock.yaml` 与 `Cargo.lock` 生成完整传递依赖 CycloneDX 1.5 JSON SBOM。
   - 接入 CI `test` 作业（保持既有 5 个顶层 job 名称与 `if:` 守卫不变）。

4. **D7 / F14 本地默认脱敏诊断包导出（`src/tianshu/diagnostics_export.py` + `tianshu data export-diagnostics`）**
   - 零网络客户端导入（纯本地导出 `.zip`，禁止落入 `data_root` 内部，`O_EXCL` 防止覆盖既有归档，绝不上传）。
   - 默认经 `credentials.redact()` + `Path.home()`/`data_root` 子路径整段消除 + POSIX/Windows 绝对路径打码（`<PATH_REDACTED>`），剥离任务 `goal`、消息正文与工具原始载荷；控制面 RPC 强制默认脱敏，支持 `preview_diagnostics()` 先预览清单与逐节勾选导出。

## 验证命令

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen python scripts/check_licenses_and_sbom.py --out /tmp/tianshu-sbom.cdx.json
uv run --frozen pytest -o addopts="" -q tests/test_diagnostics_and_sbom.py tests/test_control_settings.py tests/test_ci_workflow.py tests/test_data_cli.py tests/test_control.py tests/test_host.py tests/test_daemon.py tests/test_scheduler.py
pnpm --dir ui run typecheck && pnpm --dir ui run test && pnpm --dir ui run build
```
