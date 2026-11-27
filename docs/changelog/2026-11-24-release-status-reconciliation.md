# 2026-11-24 发行状态对账

> 范围：仅发行状态文档，不改 PRD 契约、产品代码或历史验收结论。
> 基线：`main = c5fcff0715a02a89704581ffdc0a15c5789705f6`，对账时 `origin/main` 同该提交。

## 已核实事实

| 对象 | 证据 | 结论 |
|---|---|---|
| `e87a38c` 预览发行 | [release run 44892061206](https://github.com/lorek666/tianshu-agent/actions/runs/44892061206) | `workflow_dispatch` 成功；`version=0.1.1`、`macos_arm64_preview=true` |
| `v0.1.1` 草稿 | [GitHub prerelease draft](https://github.com/lorek666/tianshu-agent/releases/tag/v0.1.1) | 未公开；六项资产；`runtime_smoke=pass`、`device_verification=unknown`、`signed=false` |
| `e87a38c` 主干 CI | [main push CI 44890059535](https://github.com/lorek666/tianshu-agent/actions/runs/44890059535) | 九项 job 全部 success |
| 对账时的主干 `c5fcff0` | [main push CI 45078725688](https://github.com/lorek666/tianshu-agent/actions/runs/45078725688) | Windows shard0 达到 25 分钟上限，run 整体 cancelled |

## 对账结论

- **预览草稿已实跑，但未公开。** `e87a38c` 的发行 run 成功只证明该提交的单目标预览构建、资产汇总和 runner runtime smoke 已完成，不等于正式发布或用户设备验收。
- **该草稿不是当前主干候选。** `e87a38c` 的同 SHA CI 全绿不能覆盖当时 `c5fcff0` 的取消结果。此后 [#229](https://github.com/lorek666/tianshu-agent/pull/229) 与 [#225](https://github.com/lorek666/tianshu-agent/pull/225) 均已合入，`3017f09` 的 [main CI 45854841880](https://github.com/lorek666/tianshu-agent/actions/runs/45854841880) 成功；[#228](https://github.com/lorek666/tianshu-agent/pull/228) 合入后的 `34a6844` 也通过了 [main CI 45862348807](https://github.com/lorek666/tianshu-agent/actions/runs/45862348807)。主干 CI 通过不等于完成发行验收。
- **ad-hoc 资源封套不等于发布者信任。** macOS 免费 no-force ad-hoc 外层封装只修复结构完整性；`signed=false` 表示没有认证发布者证书签名，不表示 Mach-O 中绝无 linker/ad-hoc 签名位。它不是 Developer ID 签名、公证或 Gatekeeper 放行证明。
- **四目标与真机仍未验收。** 本次单目标预览不证明 Intel Mac、Windows x64/ARM64、Linux 或四目标正式发行可用；CI runner 也不替代用户设备。

## 仍未完成

- Apple Silicon Mac 上的下载字节校验、首次安装、启动环境凭据读取与核心任务、手动升级/失败回退、卸载保留数据与备份恢复。Keychain 已于 2026-11-22 停用，不再是发布条件；见[环境变量凭据契约](../credential-env-contract.md)。
- 最终候选 SLO、资源采样及适用的断连/重连、取消、重启和零孤儿检查。
- 真实模型驱动 UI 的端到端验收。
- 四目标正式发行实跑与各目标用户设备验收；认证发布者签名、公证和自动更新未交付。

旧 `0.1.0` 草稿的原生 codesign 失败、历史组合候选的 CI 红及后续修复证据均保留；本次对账不用新草稿的成功覆盖旧失败，也不把旧绿继承给当前主干。
