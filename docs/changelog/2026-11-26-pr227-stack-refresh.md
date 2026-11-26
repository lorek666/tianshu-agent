# PR #227 stacked refresh（2026-11-26）

## 刷新对象

- 旧 head：`28dc2b9567131af5015e71ecd6248b055da0c076`。
- 正常双父合并 head：`534859ad89957d07f7ee990b336f851de8d9d2ac`。
- tree：`83734c91507fc0da648e43569f8572334713ee0a`。
- 双亲：`28dc2b9567131af5015e71ecd6248b055da0c076` 与
  `d8b47a75affd9ebc5ca004f8ed88d479f97acb8e`（#225 hosted-green head）。
- 未 rebase、未改共享历史、未合并 PR。相对第二父只新增 enqueue evidence 计划并修改
  `tests/test_low_disk_run_creation.py`；`src/`、`schemas/`、`ui/`、`desktop/` 零差异。

## 冻结与验证

- code tree SHA256：
  `765ad1d55837a270cb984c6027674801c691099a4c0719ca2450d3cf25ffd983`。
- review tree SHA256：
  `4a58a703cba6b9baf2c0c7a6edc3c58203a46f05073118329a9dc53470146f9b`。
- shard verifier SHA256：
  `8d0558eeb764eb80353da64b8e0f1bac3a32a2b3bc0d0175105bcce5af027520`。
- 无插件、无筛选的本机全量在短隔离根运行 exit 0；实时统计与跳过原因以日志
  `/Volumes/Date/tianshu-test-tmp/pr227-refresh-full.log` 为准，其 SHA256 为
  `87dda78af002d573c844e48124214c0680f1dad2d4922424428b65e830c51b63`。
- lock、Ruff、actionlint、diff 与本次改动文档的本地链接门禁通过；静态日志
  `.omc/artifacts/pr227-refresh/static-docs-final.log` 的 SHA256 为
  `a55978b7f654c87c7bf401ca07cc866b5ea4f83d83cf79deedc10427fb648700`。
- 独立第二读者在全新私有副本完成 control 基线、受影响域回归与有效变异负控，结论为
  **PASS**，P0/P1/P2 均无。题面指定的空 `ScriptedModel` 仍会自动 `end_turn`，不能构成
  有效负控；复审者保留该 GREEN 记录后改用空 `RecordedModel`，在 Run 状态行为断言处
  取得 RED，恢复原字节后 GREEN。报告 `.omc/artifacts/pr227-refresh/review/review-addendum.md`
  的 SHA256 为 `c0bffb1ca4e3edd9508e5d712a1cc9e52d65a974c63f07d37a3ddf933fb0a2de`。
- 作者另建全新私有副本逐文件核对冻结 manifest，并回放 control 临界节点；日志
  `.omc/artifacts/pr227-refresh/review/fresh-parent-replay.log` 的 SHA256 为
  `021131b5b78532e5c1973ee02d1188360e5e73a28a8b36ee4ba420ccbaeba004`。
- 文档收口 head `d55568f928d7d2b09bba167b41da05fb72871235` 的 hosted run
  [`45762177178`](https://github.com/lorek666/tianshu-agent/actions/runs/45762177178) 已完成，
  12 个 job 全部 success；`test (ubuntu-latest) · py3.12`、
  `test (macos-latest) · py3.12`、`test (windows-latest) · py3.12` 与
  `ui · typecheck / test / build` 四项 required checks 均为 completed/success。
  GitHub synthetic merge SHA 为 `6474a37998fd7bcb394226be2d451ba5b4bd933d`，PR 保持 open、
  mergeable/clean。run/check-runs/PR 证据 JSON 的 SHA256 分别为
  `bde43ad6df54f816fe5f286a4d432e2c568cad204e21252fc3abe27aec54cbdd`、
  `3c306357b0eee2c0e1b70377a9ff74760f4a112adf87858dc187a1eecf8e0823` 与
  `7dfcbb0abc271ddf25e5c41fe1c716746e460e503ce89d35f3e84c159ccf44bc`。

## 门禁边界

本记录证明上述冻结 code/review tree 的本地、独立复审与最终 head 的 hosted required CI。
纯文档回填未改变 code/review tree；#225 的 hosted run `45756307163` 只证明其自身 head，
不替代 #227 的 run。测试临时数据根与真实 Keychain、生产凭据和全局锁隔离。
