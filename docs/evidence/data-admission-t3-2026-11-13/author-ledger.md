# T3 author ledger

- 输入：HEAD `585e3474b7f9f03bb118308470fac1f500c3a02e` + T2 冻结 `dddec59d87775811550493c535efee2fdc30faff199c41bb6bdbf9c80598c68a`，Task3 专用授权；未同步 main、未碰 stash、未 full/commit/push。
- T2 独立：父会话已核 `PASS_TASK2_CONTENT_ONLY`、未决0；本文不重新批准T2，也不把它当T3或最终组合批准。
- 初次测试先行：真实 CLI 首 RED 25 failed / 26 passed（7.13s），均因 parser 未暴露 migrate；只证明入口缺失。源/测试、完整原始log与hash保全，不声称已到六类深层故障或并发路径。
- 测试夹具裁定：真实 events.schema 中 payload 为第8列而不是payload_ref第9列。首RED未到该断言；接线前将该断言改为显式 SELECT payload，不改产品。EventLog.append 真实返回int，不需改child序列断言。
- 最小产品接线：只加parser参数、调用T2 migrate、捕获AdmissionRefused.code、退出码与输出；DataFailure仍是Literal。成功code=null，JSON保留原报告+message；文本用to_text且统一redact。未捕获编程异常为业务错误。
- 首GREEN：51 passed（8.95s），六单故障路径均到真实域错误且移除后正控成功；双进程真实锁屏障、四原名硬退77、续做/再调用0、真实进度rename/SQL失败均通过。
- 最后边界：增加missing/empty/custom_db三种布局不初始化与RuntimeError/ValueError两种编程异常传播，56 passed（8.70s）。链接只有平台真实无法构造时skip并携原生原因，本机最终没有skip。
- 最终冻结：两文件56 passed（9.02s）；lock/ruff/diff与运行环境命令均真实0。源/测试运行前后hash一致，raw和JSON各自保留。
- 范围：463项现存输入中459个非授权基线对象不变；T2除获准§2/§8追加的知识库以外24原件不变；知识库去掉两条T3新增段落恢复精确T2 hash。旧test_data_cli只改原末尾入口门禁，之前内容字节相同。无其他debug/恢复/Host/UI变更。
- 文档：维护指南修正format2位置、实际EventLog准入、显式迁移和历史W4与本机候选的界线；KN只在§2/§8新增，T2记录/归档原件不改。
- 未决：T3独立第二读者；父同步实际main后最终组合full；正式提交绑定与四required原生CI。只交本机候选，不自批T3、不宣称G3交付或Windows设备已验。
