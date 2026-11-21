# OpenAI Responses 独立复审归档

本目录保留独立第二读者的原文，不用最终通过覆盖历史失败。
共同基线为 `71eb57ef1544d126cc929b9172f5463a14999359`；
实现与完整门禁见[变更记录](../../changelog/2026-11-21-openai-responses.md)及
[PR #209](https://github.com/lorek666/tianshu-agent/pull/209)。

- [首轮报告](REPORT.md)：`8637a4f5bf7eca31ced99bdce3bb5fdb6007a965`，
  tree `2ddd8ebd35bbc7dbb0b13c738bfbe57cba0028e8`，结论 `blocker`。
- [第二轮报告](FOLLOWUP-REPORT.md)：`6e9bf340f7c7fe728f0eb19f44818b799ad1369d`，
  tree `c7a973da0f17d007abb4a6709f184094349a6359`，结论仍为 `blocker`。
- [最终增量复核](FINAL-REPORT.md)：`b7c3773a7dcd310aafe765f2c456313a59742a2c`，
  tree `eed4d8fd25f9b25745cb8a2469fbc794a0faeaab`，结论 `PASS`，
  两项剩余根因均为 `fixed / 已修`。
- [仅测试修正附录](TEST-ONLY-ADDENDUM.md)：`fccd469325558dae9bc176a88f766b777614f217`，
  tree `d6eb0cf9757ad07efa36771388b8e654f0c14eba`，结论 `PASS`。
  独立核对版本断言的红绿与负控；除目标测试外与已审产品树无差异。
- [原始探针](PROBES.md)、[原始输出](OUTPUTS.md)与[原始校验清单](CHECKSUMS.md)
  保留逐文件 SHA-256；最终扩展探针直接保留在最终报告的 Python 代码块中。

报告原文中的路径、命令和文件名指向复审时的独立工作树。
本目录将脚本和日志装入 Markdown，避免文档归档改变已审的可执行代码树；
三份报告和测试附录自身逐字不变。原清单中的 `ui-dist` 是首轮独立重建产物，未重复提交，
其字节与同提交的 `ui/dist` 已由首轮报告核对。运行时应使用报告绑定的提交、
临时数据根、真实 `tests/conftest.py` 与合成 env 凭据，不能读取日常账号的真实凭据。

可在仓库根执行下列命令，将归档中的原始文件提取到 ignored 目录并核对字节；
它只提取材料，不运行测试，不调用模型：

```sh
python3 - <<'PY'
import hashlib
import re
from pathlib import Path

source = Path("docs/evidence/openai-responses-2026-11-21")
target = Path(".omc/artifacts/responses-review-extracted")
target.mkdir(parents=True, exist_ok=True)
pattern = rb"<!-- source-file: ([^;]+); sha256: ([0-9a-f]+); bytes: ([0-9]+) -->\n```[^\n]*\n"
for name in ("PROBES.md", "OUTPUTS.md", "CHECKSUMS.md"):
    content = (source / name).read_bytes()
    for match in re.finditer(pattern, content):
        filename, expected, length = match.groups()
        data = content[match.end():match.end() + int(length)]
        assert hashlib.sha256(data).hexdigest().encode() == expected
        (target / filename.decode()).write_bytes(data)
for name in ("REPORT.md", "FOLLOWUP-REPORT.md", "FINAL-REPORT.md", "TEST-ONLY-ADDENDUM.md"):
    (target / name).write_bytes((source / name).read_bytes())
print(target)
PY
```

独立结论不代替全量 pytest、hosted CI 或合并门禁。官方及网关付费 smoke、
Windows 用户设备、物理断电仍未验证；进程故障注入与受控传输不能为这些范围背书。
