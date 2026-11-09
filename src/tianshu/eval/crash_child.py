"""崩溃 case 的子进程入口：正常跑一次，在故障点硬退。

为什么必须是子进程：故障注入用 `os._exit`，不是异常——它要模拟断电，不给
任何清理代码机会。父进程（评测器）随后打开同一个事件库与快照根，走真实的
`recover_run` 路径。在同一个进程里"假装崩溃"只会测到恢复函数本身，测不到
"崩溃之后程序还能不能起来"。

坐标约定与 `runner.py` 一致：db 的 `blobs/` 与快照的 `data/artifacts/` 都由
父进程预先算好，子进程只按同一约定拼路径——两边各写一套必然错位。

用法（父进程构造，人不要手敲）：

    TIANSHU_FAILPOINT=after_write \\
      python -m tianshu.eval.crash_child R2 <ws> <events.db> <data_root>
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..events import EventLog
from ..loop import Limits, run
from ..model import ScriptedModel
from .cases import CASE_BY_ID


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4:
        sys.stderr.write(
            "用法：python -m tianshu.eval.crash_child <case_id> <ws> <events.db> <data_root>\n"
        )
        return 2
    case_id, ws, db, data_root = args[0], Path(args[1]), Path(args[2]), Path(args[3])
    case = CASE_BY_ID.get(case_id)
    if case is None:
        sys.stderr.write(f"未知 case id：{case_id}\n")
        return 2
    log = EventLog(db, blob_dir=db.parent / "blobs")
    try:
        run(
            task=case.task,
            workspace_dir=ws,
            out_name=case.out_name,
            model=ScriptedModel(list(case.script)),
            log=log,
            limits=Limits(case.max_turns, case.max_total_tokens),
            data_root=data_root,
        )
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())