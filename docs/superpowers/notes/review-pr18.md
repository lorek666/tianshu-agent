# PR #18 复审：M3 沙箱后端接缝（`feat/m3-sandbox-backend` → `main`）

- 复审方式：独立复审，分离 HEAD worktree `/tmp/ts-rev18`（4218e8c），**未改** `src/` `tests/` 文档，未提交；验证脚本在 `/tmp/ts-rev18-verify/`。
- 复审对象：4 个提交（2c2ad18 抽接缝 → 165baee 契约测试 → 74fd60c 收敛看盘 → 4218e8c 文档），13 文件 +1544/−93。
- 环境：CPython 3.12.13；`cd /tmp/ts-rev18 && uv run pytest -o addopts="" -q` → **496 passed, 1 skipped**。

**两条判据先落地**

| 判据 | 命令 | 结果 |
|---|---|---|
| 主循环 0 改动 | `git -C /tmp/ts-rev18 diff origin/main -- src/tianshu/loop.py \| wc -l` | **0** ✅ |
| 第二个后端 + 同一套契约 | `tests/test_backend_contract.py` 的 `env` fixture 按 `local_fs` / `memfs` 参数化 | 两后端各跑一遍，全绿 ✅ |

**结论：阻断 0 条 / 建议 12 条 / 已核查无问题 10 条。可以合并。**

阻断为 0 的理由：我没找到会削弱越界防护、会产生"审批读真盘 / 写入进虚拟盘"、或会让 local 后端已有历史数据不可核对的问题。留下的都是**契约断言的洞**（S2/S3/S4）、**测试环境不封闭**（S1/S1b）、**文档/记账不完整**（S5/S7/S8/S10/S11），以及一条**PR 之前就存在**的目录目标崩溃（S6）。如果团队把"契约测试全绿"当作合并门禁的机械证据，S1 是最该在合并前修的一条（一行 fixture）。

---

## 一、阻断（不能合并）

**无。**

---

## 二、建议

### S1【高】`conftest.py` 只把后端复位到"按环境变量解析"，没有钉住 local —— 环境里一旦有 `TIANSHU_SANDBOX_BACKEND`，全套 95 个用例变红

- 位置：`tests/conftest.py:15-21`；`src/tianshu/sandbox.py:314-317`（`reset_backend` 的语义就是"回到按 env 解析"）。
- 复现：

  ```
  TIANSHU_SANDBOX_BACKEND=memfs uv run pytest -o addopts="" -q
  → 95 failed, 401 passed, 1 skipped
  ```

- 症状：失败集横跨 `test_dispatch / test_run_control / test_snapshot / test_m2_acceptance` 等**与本 PR 无关**的用例，报错是
  `WorkspaceViolation: memfs 后端只接受虚拟根（/__tianshu_memfs__ 之下），收到真实路径：…`。看起来像 M1/M2 回归，实际是开发者 shell 里给新特性做的实验。
- 影响：M3 的退出条件是"契约测试全绿"。现在"全绿"依赖**环境里没有这个变量**，不是仓库自身的性质——验收证据不封闭。CI 干净所以合并不会立刻出事，但这是最便宜、最该先补的一条。
- 建议：fixture 里钉住默认后端，而不是只 reset：

  ```python
  @pytest.fixture(autouse=True)
  def _reset_sandbox_backend(monkeypatch):
      monkeypatch.delenv(sandbox.ENV_VAR, raising=False)
      sandbox.configure_backend(sandbox.DEFAULT_BACKEND)
      yield
      sandbox.reset_backend()
  ```

  需要 memfs 的用例自己 `configure_backend(...)`（`test_backend_contract.py` 已经是这么做的，不受影响）。

### S1b【中】`_MEM` 单例的文件内容不在 `reset_backend()` 的清理范围内 —— 同一进程里的 memfs 用例共享一个"永不消失的磁盘"

- 位置：`src/tianshu/sandbox.py:271`（`_MEM = MemBackend()` 在 import 时创建）、`:314-317`（`reset_backend` 只置 `_current = None`）、`:289-290`（`configure_backend("memfs")` 恒返回同一个实例）。
- 复现（`/tmp/ts-rev18-verify/q5_global_state.py`）：

  ```
  reset 后还能读到上一个用例写的文件: True b'from test A'
  reset 后 require_root 是否仍认这个根（_dirs 累积）: True
  ```

- 现状：契约测试给每个用例传 `MemBackend(namespace=tmp_path/...)` 新实例（`tests/test_backend_contract.py:132`），所以**现在没有实际红的用例**；但任何以后写 `configure_backend("memfs")`（或 `TIANSHU_SANDBOX_BACKEND=memfs` 再走 `run()`）的用例都会看到上个用例的文件。
- 漏了会怎样（具体症状）：第二个用例 `create_report` 同名文件 → `OverwriteRefused: 目标已存在，拒绝覆盖`；或 `list_files` 列出上个用例的文件；`_dirs` 累积还会让"本该不存在的目录"看起来存在，从而掩盖 S4 那类目录语义问题。
- 并行 vs 串行：目前没有 xdist（`pyproject.toml` 只有 pytest/ruff）；即使加，`_current`/`_MEM` 都是进程内状态，每个 worker 独立，不会跨进程串。**唯一风险面是串行**（同一进程的用例顺序），而 conftest 只覆盖了"选择"、没覆盖"内容"。
- 建议：`reset_backend()` 里把 `_MEM` 的 `_files`/`_dirs` 清回初始状态，或让 `configure_backend("memfs")` 返回新实例；同时给 `reset_backend` 的 docstring 补一句"只复位选择，不复位内容"，避免下一个人误以为已经干净。

### S2【中】契约测试对"后端撒谎"还有三处洞：`is_file` 恒真、目录 `size` 任意、`exists` 看不见目录 —— 三种植入全套绿

- 位置：`tests/test_backend_contract.py:281-289`（读目录只断言 `failed`）、`:290-304`（`list_files` 只断言文件 size，`sub` 只断言 `is_dir`）、`:191-195`（`_mkdir` 用 `.keep` 绕开"空目录"）。
- 复现（`/tmp/ts-rev18-verify/pert/pert_plugin.py`，`PERT=<名字>` 注入后只跑契约文件）：

  | 植入的破坏 | 契约测试 |
  |---|---|
  | `MemBackend.exists` 恒 True | **红**（`test_create_roundtrip`） |
  | `write_bytes_atomic` 截断到 8 字节 | **红**（`test_read_limit`） |
  | `unlink` 空操作 | **红**（`test_delete_reports_absent_after_state`） |
  | `require_root` 空操作 | **红**（`test_workspace_root_follows_backend_rules[memfs]`） |
  | `identity_key` 恒常量 | **红**（`test_lease_covers_paths_in_the_subtree_only[memfs]`） |
  | `is_file` 恒 True | **绿**（洞） |
  | `list_dir` 目录 size 恒 42 | **绿**（洞） |
  | `exists` 对目录恒 False | **绿**（洞） |

  命令形如：
  `PERT=is_file_always_true PYTHONPATH=/tmp/ts-rev18-verify/pert uv run pytest -o addopts="" tests/test_backend_contract.py -p pert_plugin -q` → `65 passed, 1 skipped`。
- 为什么是洞：
  1. `read_text` 的 `is_file` 门（`tools.py:277-278`）与"读失败"（`tools.py:280-284`）都落到 `failed`，契约只看得到 `failed`，所以门烂掉也没人知道；
  2. 目录 `size` 从来没有断言过（见 S3）；
  3. **"文件与目录同名"从未被构造**：`create_report` 指向一个已存在的目录时，`_create_report` 的 `io.exists`（`tools.py:317`）是唯一的拒绝点，`exists` 对目录失效就会在 memfs 上把目录"覆盖"成文件，local 上则抛 `IsADirectoryError`。
- 建议：加三条低成本断言——(a) 读目录时 `output["error"] == "不是文件"`（而不是"读取失败"）；(b) 目录条目的 size 要么断言、要么在文档里声明"不承诺"（S3）；(c) 构造一次"名字与目录同名"的 `create_report` / `modify_text`，断言 `rejected`/`failed` 且目录仍在。
- 注：C0 的两个元测试（`tests/test_backend_contract.py:586-622`）本身是**对的**——它们和参数化用例共用 `assert_create_roundtrip` / `assert_read_limit`，不是另写一套更弱的检查；上面 5 种"红"也证明这套断言真的会响。这条不是否定 C0，而是说**金的覆盖面只有两条路径**。

### S3【中】memfs 目录 `size` 恒 0、local 是 `st_size`（本机 64）—— 两个后端的模型可见输出不一致，既没断言也没进文档

- 位置：`src/tianshu/sandbox.py:220`（`out.append((name, True, 0))`）vs `src/tianshu/sandbox.py:114-123`（local 用 `st.st_size`）；契约 `tests/test_backend_contract.py:290-304` 没有断言目录 size。
- 复现（`/tmp/ts-rev18-verify/q4_behavior.py`、`q4_memfs_listing.py`）：

  ```
  local  list_files: [('a.md', False, 1), ('sub', True, 64), …]
  memfs  list_files: [('a.md', False, 1), ('sub', True, 0)]
  ```

- 影响：等 memfs 真的跑到某个 Host 上（或有人据此写 UI/摘要），"目录多大"会随后端跳变；`size: 0` 还可能被模型读成"空目录"。
- 建议：契约里显式钉一个值（推荐目录一律 `0`，两个后端都改成 0，因为目录字节数本来就没意义），或至少在附录 B 的覆盖矩阵加一行"目录 size 不承诺"。我倾向**改 local**——这是 M2 遗留的噪声，不是历史数据。

### S4【低】memfs 无法表示"空目录"，契约用 `.keep` 变通把它盖住了

- 位置：`tests/test_backend_contract.py:191-195`（`_mkdir` 对 memfs 写 `sub/.keep`）；`src/tianshu/sandbox.py:173-186, 251-257`（目录只来自 `require_root` 或写文件的父链）。
- 复现（`/tmp/ts-rev18-verify/q4_empty_dir.py`）：local 上一个用户建的空目录 `docs` 在 `list_dir` 里是 `('docs', True, 64)`；memfs 上 `is_dir(docs)` 为 False，只有 `require_root(docs)` 或往里写文件之后才出现。
- 工具可见后果：`create_report("docs/x.md")` 在 local 上只要 `docs` 存在就行，在 memfs 上会被 `dispatcher.py:899-901` 的 `dispatch.no_parent` 拒掉（除非 docs 恰好是某个绑定根）。契约之所以全绿，是因为 `_mkdir` 先往目录里塞了 `.keep`。
- 建议：要么给 `FileBackend` 加 `mkdir`（真实的后端能力差异会立刻暴露），要么在覆盖矩阵里写"memfs 不承诺空目录"，并把 `_mkdir` 的变通注释出来。

### S5【中】PR 自报的目录目标崩溃：复现确认，**PR 之前就存在**，但建议顺手修（修法很便宜）

- 位置：`src/tianshu/tools.py:59-72`（`observe_state` 对目录 `exists→True` 然后 `read_bytes` 炸）；`src/tianshu/dispatcher.py:1358-1362`（`build_request` 调它）；`src/tianshu/loop.py:828`（`_step` 里没有任何局部 try）→ `src/tianshu/loop.py:543` 的兜底把整个 run 判 failed。
- 复现（`/tmp/ts-rev18-verify/q6_dir_target.py`，脚本第 9 行起就是完整可跑用例）：

  ```
  PYTHONPATH=/tmp/ts-rev18/src .venv/bin/python /tmp/ts-rev18-verify/q6_dir_target.py
  → outcome='failed', error="主循环异常：IsADirectoryError: [Errno 21] Is a directory: '…/ws/sub'"
  ```

  （模型脚本：`delete_file {"path": "sub"}`，`sub` 是目录。`modify_text` 指向目录同样触发；memfs 下换成 `FileNotFoundError`。）
- 与自报的对照：`docs/superpowers/notes/m3-backend-seam-proposal.md:511-518` 的描述**准确**（"既不是 rejected 也不是 failed、M2 起就是这样"），我把同一脚本指向 `main` 的 `src/` 也复现了同一行错误，确认不是本 PR 引入。
- 严重度：**中**。无字节副作用（`effect_state=none_applied`、无 receipt），代价是**整轮 run 被判 failed**（`turns=1`），而且模型/提示词注入可以稳定触发（例如让模型 `delete_file {"path": "."}`）——对一个"安全边界"项目，把一个可被内容注入触发的 run-killer 留到 PR G 有点久。附带：文档只写了 `delete_file`/`modify_text`，`restore_file` 同为 `ACTION_OVERWRITE`（`tools.py:554`，undo 路径 `cli.py:692-700` 同样走 `build_request`），也在这个坑里。
- 建议（比文档里"返回可判定状态"更便宜）：`build_request` 里对 `ACTION_OVERWRITE/DELETE` 先判 `io.exists(target) and not io.is_file(target)`，抛 `ToolNotAllowed`（会被 `dispatcher.py:626-640` 映射成 `rejected/none_applied`，模型可以换工具重试），一个字节都没动。

### S6【低】读失败/列目录失败的语义从"异常 → unknown/阻断"变成了 `failed/none_applied`（对 local 也是），没写进附录 B 的差异清单

- 位置：`src/tianshu/tools.py:261-262`（`_list_files` 捕获 `OSError`）、`:283-284`（`_read_text`）、`:353-355`（`_modify_text`）、`:392-394`（`_delete_file`）。
- 旧行为：`target.iterdir()` / `target.read_bytes()` 没有 try，`PermissionError` 一类会逃出工具 → `Dispatcher` 判 `unknown` 并阻断工作区；新行为：`failed / none_applied`，不阻断。
- 判断：**新行为更对**（读失败什么都没改，不该记 unknown），并且有新契约 `tests/test_backend_contract.py:691-702` 钉住；但它确实是对 local 的静默行为变化，附录 B 的"与提案的差异"只写了"`read_text` 一次打开消掉竞态"，没写这条异常映射。建议在 PR 描述/附录 B 补一行（审计口径变化值得留痕）。

### S7【低】`read_text.bytes` 的语义现在被跨后端契约固定了，但没有任何文档定义它

- 位置：`src/tianshu/tools.py:305`（`"bytes": len(raw)`）；`tests/test_backend_contract.py:267-269` 断言"回退一个字节后 `bytes` 仍等于 65536"。
- 实测（main 与分支完全一致，**不是行为变化**）：

  ```
  read_text(bytes, total_bytes, len(content.encode())) = 65536 65538 65535
  ```

  即 `bytes` 是"从盘上读到的原始前缀长度"，在 UTF-8 回退场景里**不等于** `content` 的字节数。提案 §8 第 8 条只定义了 65536 整的情况。
- 建议：在 tool 描述或提案里写明 `bytes = 本次从盘读取的字节数（截断后），content 可能少 1–3 字节`；否则下一个维护者会"修正"成 `len(content)` 并把契约测试弄红。

### S8【低】`MemBackend.list_dir` 不持锁遍历，`write_bytes_atomic` 持锁 —— 并发时可能抛 `RuntimeError`

- 位置：`src/tianshu/sandbox.py:206-221`（遍历 `self._files` / `self._dirs`，无 `self._lock`）vs `:232-237`（写入持锁）。
- 后果：另一个线程写入时 `dictionary changed size during iteration` → `tools.py:261` 只捕 `OSError` → 逃到 `loop.py` 的 `_exec_read` 兜底，变成一次 `failed`（不会崩）。当前主循环是串行的，所以只是"锁加了一半"。
- 建议：`list_dir` 也在 `with self._lock:` 里拷贝一份快照，或干脆声明该后端非线程安全。

### S9【低】`MemBackend.require_root` 每次都 `self.namespace.exists()` 读宿主盘，与模块文档表格"不会碰宿主盘"字面不符

- 位置：`src/tianshu/sandbox.py:180` vs `:11-14`（表格"会碰宿主盘吗"）。
- 说明：它确实**一个字节都不写**（这点实测成立：`tests/test_sandbox_backend.py:107-116`，我也复验了宿主文件对 memfs 不可见），但每次 `Workspace` 构造都会 lstat 一次宿主路径。fail-closed 设计本身正确。
- 建议：表格那行改成"不写；只读一次'命名空间是否存在'"。顺带提一句：宿主上真有人建了 `/__tianshu_memfs__` 时 memfs 全线拒绝运行（有意），值得在文档里点明这是全球开关而不是本地开关。

### S10【低】"这条 run 用的是哪个后端"没有可核对的印记（receipt 有，intent/dispatch/snapshot 没有）

- 位置：`src/tianshu/dispatcher.py:1304`（receipt 记 `backend`）、`src/tianshu/recovery.py:271`（恢复时记**当前**后端名）；`tool.intent` / `dispatch` / `SnapshotStore.Change` 都不带后端。
- 现状安全性：跨后端恢复基本是 fail-closed——`recovery.py:108` 的 `Workspace(real_path)` 在 memfs 下直接 `WorkspaceViolation`；反向（memfs 的 run 用 local 恢复）会看到真盘"仍是写前状态"，结论"未执行"对真盘而言是真的。
- 建议：M5 的 per-run Host 之前，把 `backend` 写进 intent 或 dispatch 记录，让"恢复时判定的世界"与"执行时的世界"可被机械核对，而不是靠 `require_root` 顺带挡住。
- 补充（不用改）：`dispatcher.py:397-399, 552-554` 的 `check_request_preconditions` 没显式传后端，走的是 `current_backend()`；一旦后端在审批之后被换掉，`dispatcher.py:626-640` 会把它判成 `rejected/none_applied`，`FileExecutor` 的双检（`tools.py:636-644`、`782-790`）也兜住了，所以**不会**出现半真半假。只是将来做 per-run 后端时这两处必须一起改。

### S11【低】`tests/test_dispatch.py` 里其实没有越界用例（任务描述里的"原有用例"在别处）

- 事实：`git grep -n "\.\./secret" origin/main -- tests/` 只命中 `tests/test_acceptance.py:141-148`（A4 提示词注入越界）；绝对路径只有 `tests/test_permissions.py:129`（策略层）。符号链接**此前完全没有** escape 用例（`tests/test_leases.py` 只有 identity 用例）。本 PR 新增的 `tests/test_backend_contract.py:337-347` 是第一条 symlink escape 测试。
- 建议：只是提醒后续引用证据时别写成"test_dispatch 原有覆盖"；顺带可以把 A4 的注入用例扩一条"写工具指向目录"的变体（正好覆盖 S5）。

---

## 三、已核查无问题

### V1 越界防护没有变弱：local 后端三类越界全部仍被拒

- `Workspace.resolve` 本体**一行没改**（`src/tianshu/workspace.py:155-167`，仍是 `resolve()` 跟随 realpath + `root not in resolved.parents` 判定）；拆出去的只有"根现在存不存在"（`:150-153` → `LocalBackend.require_root`，`sandbox.py:98-100`，与 M2 的 `is_dir()` 等价）。
- 复现（`/tmp/ts-rev18-verify/q1_boundary.py`）：**17 条全绿**
  - `../` 三种写法（`../../../etc/passwd`、`../../outside/secret.txt`、`sub/../../outside/secret.txt`）；
  - 绝对路径两种（`/etc/passwd`、外部绝对路径）；
  - symlink 五种：目录指向根外（`linkdir/secret.txt`、`linkdir` 本身、尾部不存在的 `linkdir2/new-file.md`）、文件指向根外、悬空链接指向根外；反向两条（根内 symlink、symlink 当 workspace 根）仍被允许；
  - 工具层 `read_text` 五种同形输入全部抛 `WorkspaceViolation`（不是 failed）。
- 写入路径（脚本里 3 条 `create_report` 被"写工具必须经派发"提前拦成 `ToolNotAllowed`，改用 Dispatcher 复核，`/tmp/ts-rev18-verify/q1b_dispatch_boundary.py`）：两个后端上 `modify_text` / `delete_file` / `create_report` 指向根外目标，全部 `rejected=True rule='dispatch.lease_lost'`，根外文件未被改动。**这是第二道独立门**：即使绕过 loop 直接构造请求，租约覆盖（`leases.py:556-562`）也会拒。

### V2 memfs 的"不承诺 symlink"被写成了测试标记（skip），不是只写在注释里；但它不是断言

- `tests/test_backend_contract.py:337-339`：`if env.backend.name != "local_fs": pytest.skip("memfs 没有符号链接，不承诺这条")`，`-rs` 输出可见：`SKIPPED [1] tests/test_backend_contract.py:339: memfs 没有符号链接，不承诺这条`；附录 B 覆盖矩阵也单列了一行。
- 我的补充实测（`q1c_memfs_symlink.py`）：宿主盘上真造一个 `mem/ws/link -> /etc` 之后，memfs 的 `Workspace.resolve("link/passwd")` **也**会抛 `WorkspaceViolation`（因为 `resolve()` 走的是宿主 realpath），宿主 `real.txt` 对 memfs 不可见。也就是说 memfs 这条其实"顺带被挡住"，但没有任何断言保证它——它是**未覆盖**，不是**已验证**。这与文档口径一致，不构成问题；只是别把 skip 读成"已验证不承诺"。

### V3 "看盘逻辑收敛"确实收敛了：工作区数据路径没有绕过探针的地方

- 全 `src/tianshu/*.py` grep `os.stat|st_|exists()|is_dir()|is_file()|read_bytes|read_text|open(|iterdir|unlink|write_bytes|stat()`，除后端自身外，剩余宿主盘访问都能逐条归位：
  - `src/tianshu/events.py:243-244` 事件 payload blob（授权/证据库，故意在宿主盘）；
  - `src/tianshu/snapshot.py:82,100-101,111`（blob/manifest，附录 B/`snapshot.py:176-189` 注释写明"证据不跟着工作区虚拟化"）、`:199-202`（回滚前读 blob）；
  - `src/tianshu/leases.py:724-726`（锁文件）、`src/tianshu/fsutil.py`（原子写）、`src/tianshu/model.py:251`（网络）；
  - `src/tianshu/sandbox.py`（两个后端自己的实现）。
- 工具/dispatcher/snapshot/recovery 全部走 `ctx.io` / `current_backend()`：`tools.py:71, 282, 353, 392, 398, 428`；`dispatcher.py:406, 899, 1358-1362`；`snapshot.py:176-189, 214-222`；`recovery.py:57-64, 205, 271`。
- **没有发现"审批读真盘、写入进虚拟盘"的组合**：审批绑定值（`observe_state`）与写前复核（`check_request_preconditions`，`tools.py:851`）用的是同一个 `FileBackend`；executor 构造时（`tools.py:636-644`）与每次执行前（`:782-790`）都校验后端没被换过，换过就 `ToolNotAllowed`（→ `rejected/none_applied`，一个字节没动）。
- 唯一"看盘"但不在关键词里的是 `Workspace.resolve` 的 `Path.resolve()`：它每次都对宿主做 lstat/realpath（memfs 也如此）。这是边界计算的必要输入，不产生半真半假状态（只读元数据、且方向是 fail-closed），记录在此以免被当成"已完全虚拟化"。

### V4 契约测试确实在两个后端上跑，且 C0 会响

- 496 passed / 1 skipped；`env` fixture 逐个用例参数化 `local_fs` / `memfs`（`tests/test_backend_contract.py:124-139`）。
- C0 的两个坏后端（`_SilentWriteBackend` / `_NoLimitBackend`，`:586-622`）与参数化用例共用同一批断言函数（`:204-221`），不是另写一套更弱的检查；我另加 5 种植入也都被抓（S2 表格上半）。

### V5 行为变化全部被固定成断言 + 写进了文档（NFC 排序）

- 改的是 local **也生效**的排序键：`tools.py:268-270` 用输出里的 NFC 名字排序。main 与分支实测差异（`q4_behavior.py`，macOS NFD 文件名）：
  - main：`a.md, b.md, é.md, note.md, sub`（按磁盘上的 NFD 排）
  - 分支：`a.md, b.md, note.md, sub, é.md`（按 NFC 排，与显示一致）
- 断言：`tests/test_backend_contract.py:298-301`（NFC + `names == sorted(names)` + 精确顺序）；文档：附录 B 差异第 3 条。历史数据不受影响：`replay.py` 不重算工具输出，凭据里也没有 output 哈希（`receipt.py` 只有 `args_hash` + 前后状态），旧 Run 的可核对性不依赖列表顺序。

### V6 `read_text` 的数值没有静默变化（与 main 逐项一致），`relative()` 的改动只在 Windows 生效

- 同一份输入在 main 与分支上：`bytes / total_bytes / len(content.encode()) = 65536 / 65538 / 65535`，完全相同（`q4_behavior.py`）。分支只是把"先 stat 再 open"改成"一次 open + fstat"（`sandbox.py:125-130`），消掉了竞态窗口。
- `Workspace.relative` 改成 `as_posix()`（`workspace.py:172-179`）：POSIX 上输出不变，Windows 上才从 `\` 变 `/`；`test_paths_are_relative_and_posix`（`test_backend_contract.py:447-457`）钉住。对已有数据无影响。

### V7 `fsutil` 的 mode 保留是修 bug，而且有断言；新建仍 0600

- `src/tianshu/fsutil.py:19-26, 44-47`（覆盖前取原 mode，`os.replace` 前 `chmod` 临时文件；`os.fchmod` 换成 `os.chmod` 的理由是 Windows 没有前者，成立）。
- 断言在 local-only 的原语测试里：`tests/test_sandbox_backend.py:47-56`（覆盖 0644 仍是 0644）、`:59-63`（新建 0600）。我实测把 `_existing_mode` 打成恒 `None`：`test_sandbox_backend` 红、**契约文件仍绿**——这是设计如此（memfs 无 mode，覆盖矩阵写明了 local-only），不是漏测；只是提醒"契约全绿"不覆盖 mode。
- 对历史数据的影响：旧行为会把用户 0644 文件悄悄变 0600，新行为保住原 mode；已被旧行为改成 0600 的文件不会自己变回来（无迁移，也不需要）。

### V8 快照/恢复/undo 全程走后端，证据仍在宿主盘；undo 的幂等语义等价

- `snapshot.py:176-189`（当前盘面问后端）、`:214-222`（撤销写/删走后端，`FileNotFoundError` 吞掉等价于原来的 `unlink(missing_ok=True)`）、`:199-202`（blob 读宿主盘，审计材料，文档写明）。
- `recovery.py:57-64, 205` 与 `snapshot` 同一口径；`recovery.py:205` 的 `size()` 与旧 `stat().st_size` 在"文件不存在就抛"上等价。
- 结论：没有出现"证据被虚拟化、状态在真盘"或反向的组合。

### V9 租约的跨后端承诺与实现一致

- `leases.assert_covers`（`leases.py:554-562`）同后端比 `key`（local = `dev:ino`），否则退到 `path_key` 祖先判断；`ResourceIdentity.of(backend=...)` 的注释（`workspace.py:51-64`）与实现一致。`tests/test_leases.py` 的 symlink identity 用例在本 PR 后仍全绿（496 passed 里）。
- 补充：`MemBackend.identity_key` 恒 `path:` 前缀，`ResourceIdentity.overlaps` 在"同 key"失效时仍有路径前缀这一层，没有出现"memfs 恒真覆盖一切"的退化（我植入过 `identity_key` 恒常量，契约会红——见 S2 表格）。

### V10 PR 自报缺陷准确（Q6）

- 复现输出、main 对照、严重度判断见 S5。附录 B 另外 4 条"明确没修"我也逐条对过代码：快照证据键只有相对路径（`snapshot.py:146-189` 用 `c.path`）、凭据不可独立核对（`receipt.py:99-108`，`action` 是 tool_id、`evidence` 是合成串）、exists/unlink 竞态原样保留（`tools.py:317, 398`）、`partial`/`cancelled` 是死枚举（`test_backend_contract.py:459-472` 明确断言不会出现 partial）。**自报清单可信。**

---

## 四、六个必答问题的直接回答

1. **越界防护有没有变弱？** 没有。`../`、绝对路径、指向根外的 symlink 在 local 下全部仍被拒（V1：17 条实测 + Dispatcher 层 `dispatch.lease_lost` 第二道门）；memfs 侧的"不承诺 symlink"被写成 `pytest.skip`（V2，`-rs` 可见），**不是断言**——实测它顺带也被 `Workspace.resolve` 挡住，但属未覆盖而非已验证。
2. **"看盘逻辑收敛"是否真的收敛？** 收敛了（V3）。工作区数据路径没有绕过 `ctx.io` 的读写；剩下的宿主盘访问都能归到证据/授权库或后端自身。
3. **C0 元测试真的能抓退化吗？** 能抓 5 类（S2 表格上半），但有 3 处洞（表格下半）：`is_file` 恒真、目录 `size` 任意、`exists` 看不见目录，植入后契约**全绿**。
4. **行为变化是否被知情？** NFC 排序：断言 + 文档都有，旧数据不受影响（V5）；`read_text.bytes` **没有**变化（V6），但语义无文档（S7）；memfs 目录 size 常量**没**断言也没进文档（S3）；另有一条静默的"读失败 unknown→failed"（S6）。
5. **`loop.py` 0 改动的代价？** 后端成了进程级全局量。串行测试靠 conftest 自动复位（它本身有效：抽掉 conftest 会 22 红，报 `WorkspaceViolation: memfs 后端只接受虚拟根…`），但有两处漏：环境变量通道（S1，95 红）与 `_MEM` 单例内容（S1b）。并行无害（无 xdist；状态是进程内的）。
6. **它自报的缺陷是否准确？** 准确（S5 + V10）：可复现、`main` 上同样复现（非本 PR 引入）、严重度中（模型/注入可稳定杀掉一轮 run，无字节副作用）；文档漏了 `restore_file` 也在这个坑里。

---

## 附：验证脚本清单（均在 `/tmp/ts-rev18-verify/`，不在仓库内）

| 脚本 | 用途 |
|---|---|
| `q1_boundary.py` | local 后端 17 条越界（`../`、绝对、symlink×5、工具层） |
| `q1b_dispatch_boundary.py` | 绕过 loop 直接给 Dispatcher 根外目标（两后端）+ memfs 的 `../`/绝对路径 |
| `q1c_memfs_symlink.py` | memfs 面对宿主 symlink 的实际行为、宿主文件不可见 |
| `q4_behavior.py` / `q4_memfs_listing.py` / `q4_empty_dir.py` | NFC 排序 main↔分支、bytes 语义、目录 size、空目录 |
| `q5_global_state.py` | `_MEM` 单例残留、`reset_backend` 不清内容 |
| `q6_dir_target.py` | 目录目标 → `IsADirectoryError` → run failed（分支与 main 各跑一次） |
| `pert/pert_plugin*.py` | Q3/S2 的"打断正常路径"注入（`PERT`/`PERT2`/`PERT3`） |

复跑全套：`cd /tmp/ts-rev18 && uv run pytest -o addopts="" -q`。
