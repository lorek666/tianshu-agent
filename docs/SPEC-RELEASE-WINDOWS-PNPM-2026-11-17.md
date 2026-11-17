# Windows 发布构建 pnpm 启动修复规格

## 问题与证据

精确 main `6e597807a23ba9b6a502fb14736512a92497a949` 的 release run
[`43921743059`](https://github.com/lorek666/tianshu-agent/actions/runs/43921743059)
中，macOS arm64/x64 两个目标通过，但 Windows x64/arm64 都在
`Build and package Tauri app` 步骤启动时失败：

```text
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

两份失败日志都停在 `scripts/package_app.py` 的
`subprocess.run(command, cwd=repo_root / "desktop")`，且
`pnpm/action-setup` 已成功执行。Windows 上安装的是 `pnpm.cmd`，而
`subprocess.run` 使用无扩展名的 `pnpm` 作为 argv[0]，Python 在进入 Tauri
进程前找不到可执行文件。问题发生在 Windows 发布打包入口，不是 Tauri 编译、
runtime 内容或 Windows CI 测试失败。

## 不变量

- Windows 上发布打包必须调用安装后的 `pnpm.cmd`；POSIX 继续调用 `pnpm`。
- PATH 项可以是相对目录；`which` 找到 launcher 后必须先转成绝对路径，避免
  Tauri 子进程切换 `cwd=desktop` 后重定位到另一位置。
- 找不到启动器时必须返回项目定义的 `PackageError` 并携带命令名，不能泄漏裸
  `FileNotFoundError` 或静默降级。
- 原生 OS/架构匹配、Rust triple、bundle/resource/output 封闭检查及现有
  Tauri 产物唯一性断言全部保持不变。
- 不以 cross-build、跳过 Windows、延长超时、重试或放宽发布工作流来完成修复。

## 验收

1. 先在当前实现上加入最小回归：模拟 Windows，捕获传给 subprocess 的 argv，
   断言首项为 `pnpm.cmd`；旧实现必须失败。
2. 修复后回归转绿，并以负控证明 POSIX 仍使用 `pnpm`，Windows 跨 OS/架构拒绝
   行为未被改变。
3. 相关 `tests/test_package_app.py`、全仓 lock/Ruff、项目规定的全量测试通过。
4. 新精确 PR SHA 的 required CI 全绿并经第二读者独立复审。
5. 合并后在新的精确 main SHA 重新执行四目标 release workflow；只有
   macos-arm64、macos-x64、windows-x64、windows-arm64 均产出并通过 verifier，
   assembler 才可创建 Draft Release。

## 范围外

签名、公证、自动更新、Windows 10/11 用户设备、ARM64 实机、安装升级/回退和
物理掉电仍按原规格保持未验证；本修复不改变这些结论。
