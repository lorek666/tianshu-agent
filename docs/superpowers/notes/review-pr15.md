# PR #15 独立复审 · `feat/m3-provider-seam` → `main`

- 复审对象：`9898837`（`git log main..HEAD` 共 5 个提交）
- 复审基线：`git diff main...HEAD` = 9 个文件；`src/` 下只动了 `cli.py` / `model.py`
- 复审方式：自己读 diff + 跑全量测试 + **7 项变异测试** + **用 `main` 产物做跨版本回放实验** + 用真实
  `tool_manifest()` 走 adapter 观察请求体
- 本人**未修改** `src/` / `tests/` / 文档，未提交任何 git 操作。所有实验在 `/tmp` 副本上做。

## 0. 结论速览

| 档 | 条数 | 一句话 |
|---|---|---|
| **阻断（不能合并）** | **1** | OpenAI adapter 没有把 C2 的 `input_schema` 翻译成 OpenAI 的 `parameters`，模型拿不到工具参数 schema；而新契约测试只断言工具名，绿灯把这个洞盖住了 |
| 建议 | 11 | 见 §2，均给出 file:line 与复现 |
| 已核查无问题 | 10 | 见 §3，含"主循环 0 改动"、变异测试、跨版本回放 |

判据（按 PRD 而不是按喜好）：PRD §11 → `docs/PRD-v1.md:748`「M3 可替换 … 退出条件：契约测试全绿，主循环 0 改动」；
PRD §5.1 三条新规则 → `docs/PRD-v1.md:325-332`；`ModelError` 口径 → `docs/PRD-v1.md:334-344`；
`finish_reason` 13 值 → `docs/PRD-v1.md:436-444`。**"主循环 0 改动"我验证为空 diff，这一条 PR 是真做到了。**

---

## 1. 阻断（不能合并）

### B1. OpenAI adapter 漏翻工具 schema 字段名：发的是 `input_schema`，OpenAI 要 `parameters`

- 位置：`src/tianshu/model.py:375`
  ```python
  body["tools"] = [{"type": "function", "function": t} for t in tools]
  ```
  `t` 来自 `tool_manifest()`（`src/tianshu/tools.py:530-540`），键是
  `{name, description, input_schema}`。这里只在外层套了 `{"type":"function"}`，**没有把
  `input_schema` 改名成 `parameters`**，所以发出去的函数定义里根本没有 `parameters`。

- 复现（不用联网，注入 fake Transport）：

  ```bash
  cd /Volumes/Date/天枢Agent
  PYTHONPATH=/tmp/rv15/src .venv/bin/python - <<'PY'
  from tianshu.model import OpenAICompatibleModel
  from tianshu.tools import tool_manifest
  class T:
      def __init__(s): s.calls=[]
      def post(s, url, *, body, headers, timeout): s.calls.append(body); return {"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}
  t = T(); OpenAICompatibleModel("m", base_url="http://x/v1", api_key="k", transport=t).complete(
      [{"role":"user","content":"x"}], tool_manifest())
  print(sorted(t.calls[0]["tools"][0]["function"]))
  PY
  ```

  实测输出：`['description', 'input_schema', 'name']` —— **没有 `parameters`**。

- 为什么这是阻断，而不是"顺手提一句"：
  1. 本 PR 自己把规则写进了 PRD（`docs/PRD-v1.md:327-328`）："adapter 做**内部形状 ↔ 各家线上协议**
     的双向翻译"。Anthropic 侧 `input_schema` 恰好是原生字段（`model.py:472` 直接 `dict(t)`，这是对的）；
     OpenAI 侧却当成"换个 base_url 就行"——**同一个 C2 投影，一个 adapter 翻了，另一个没翻**。
  2. 后果二选一：严格按 OpenAI 校验的端点会 400（`Unrecognized request argument … function.input_schema`，
     本机无 key，**未在真机验证**）；宽松网关（比如计划里用的中转）会静默丢掉整个参数 schema。
     丢掉 schema 之后模型只能看见工具名和描述——这与
     `docs/superpowers/plans/2026-11-09-m3-swappable.md:80-89` 记录的"真实模型给 `read_text` 漏 `path`、
     给 `create_report` 用 `path` 代替 `name`"高度吻合（**相关性，不是因果证明**）。计划把这归因为
     "工具参数命名不一致，是产品缺陷"，按代码看更可能是 adapter 没把 schema 交给模型。
  3. M3 的退出条件是"契约测试全绿"。测试确实全绿——但 `tests/test_model_contract.py:172-179` 对工具只断言了
     **名字**，`tests/test_model_contract.py:119-129` 用的还是手写 `TOOLS`，从没调用过真实
     `tool_manifest()`。**绿的是"名字对"，不是"契约对"。** 也就是说 M3 的核心判据在 OpenAI 这一侧没有被真正证明。

- 解除阻断的最小改法（本人在 `/tmp` 副本上打过并跑过，全绿）：

  ```python
  # model.py:375 附近
  body["tools"] = [
      {"type": "function",
       "function": {"name": t["name"], "description": t.get("description", ""),
                    "parameters": t["input_schema"]}}
      for t in tools
  ]
  ```
  并在契约测试里改成用 `tool_manifest()` 装配、断言 OpenAI 侧 `function["parameters"]` 存在、
  Anthropic 侧 `tool["input_schema"]` 存在（见 S7）。

- 也可以选择不接受这个阻断，但那样必须同时做两件事，否则结论是虚的：把
  `README.md:118` / 计划里"两个协议都做了真实端到端验证"的说法收窄成"经中转网关验证"，
  并在计划 §2 的缺陷记录里改掉"工具参数命名不一致"这个归因。

> 说明：这一行是 `main` 上就有的旧代码，不是本 PR 新写的。阻断针对的是**本 PR 声称"接缝已被第二个
> 真实协议证明、契约测试全绿"**这件事——它把一条既有的互操作 bug 写成了规范，还用一条测不到它的
> 测试盖了章。修它是一行，成本极低，没有理由留给下一个人。

---

## 2. 建议

### S1. 未映射结束原因：不是"更容易误伤成功路径"，但会丢掉那一轮的模型原文与 usage

- 位置：`src/tianshu/model.py:541-545`（抛 `ModelError(kind="protocol")`）、
  `src/tianshu/loop.py:612-617`（`except Exception` → 只记 `model.error`）
- 我实测了"改前 vs 改后"（`main` 代码来自 `git archive main`）：

  | 场景 | `main` 的行为 | 本分支的行为 |
  |---|---|---|
  | `finish_reason: null`（200 + 合法 content） | `_map_finish(None)` → `"unknown"`（实测）→ Run **failed**，但 `model.result` 里留下了 content | `ModelError(protocol)` → Run **failed**，`model.result` **不存在**，content 丢失 |
  | `finish_reason: "stop"` | `end_turn` | `end_turn`（不变） |
  | `insufficient_system_resource`（DeepSeek 真实语料）/ `sensitive`（GLM） | `"unknown"` → failed | `ModelError(protocol, retryable=False)` → failed |

  结论：**没有"改前成功、改后失败"的路径**——旧兜底是 `"unknown"` 而不是 `end_turn`，两条都失败。
  所以这次改动是"更诚实"，不是"更容易误伤"。但它把两层信息丢了：
  1. 模型已经给出的文本与 usage（`model.error` 里只有一个字符串）；
  2. 一个"200 + 有内容 + 结尾字段异常"的现场，事后无法复盘。

  建议：`model.error` 的 payload 里带上 `finish_reason` 原始值和 content 摘要（例如
  `{"request_id":…, "error":…, "raw_finish_reason": raw, "content_preview": resp.content[:200]}`），
  这不是契约变更，只是把已经拿到手的东西记下来。

- 复现（本分支，null 走完整主循环）：

  ```bash
  # fake Transport 返回 {"choices":[{"message":{"content":"这里是完整回答"},"finish_reason":null}]}
  # 期望输出：outcome: failed | error: 模型调用异常：未映射的结束原因（openai）：None…
  #          事件类型: [... 'model.intent', 'model.error', 'job.finished' ...]
  ```

### S2. Anthropic 的 `pause_turn` 被自己的文档列为语料，却被映射表拒绝

- 位置：`src/tianshu/model.py:428`（docstring 写"`stop_sequence` / `tool_use` / `refusal` / `pause_turn`"）
  vs `src/tianshu/model.py:530-537`（映射表里**没有** `pause_turn`）
- 后果：一旦用 Anthropic 服务端工具（web search 之类），`pause_turn` 是**正常续接信号**，不是错误；
  现在会被当成 `protocol` + `retryable=False` 硬失败。13 值里 `checkpoint` 是现成的落点，或至少把
  docstring 改成"v1 不支持 `pause_turn`"。
- 复现：`python -c "from tianshu.model import _map_finish; _map_finish('pause_turn', vendor='anthropic')"` → `ModelError`。
  （`docs/superpowers/plans/2026-11-09-m3-swappable.md:60` 说这是有意的"报错而不是猜"——有意可以，但
  docstring 与实现互相矛盾，读代码的人会以为已经支持。）

### S3. `retryable` 目前没有任何消费者

- 位置：`src/tianshu/model.py:80-91`（唯一计算处）；全仓 `grep -rn retryable src/ tests/` 只有构造与
  `repr`，没有一处读它做重试/降级。
- 这不是 bug（PRD 明说它是"建议"），但当前它只进事件日志。建议在 `docs/TECH-ARCH.md` §12.2 补一句
  "消费方（重试/降级）尚未接入"，否则下一层的人会以为策略已经落地。

### S4. `from_payload` 读不了旧日志（`RecordedModel` 的 docstring 与能力不符）

- 位置：`src/tianshu/model.py:157-166` + `135-143`；docstring 在 `model.py:18` 说 RecordedModel
  "回放用，答案来自事件日志"。
- 复现（拿 `main` 写出的真实旧 payload）：

  ```bash
  python - <<'PY'
  from tianshu.model import ModelResponse
  p = {'content':'HTTP 500: x','finish_reason':'error','tool_calls':[],'usage':{}}
  ModelResponse.from_payload(p)   # ValueError: finish_reason 越界：'error'
  PY
  ```

  `main` 版本的 `OpenAICompatibleModel` 在 HTTP 失败时**就是**写 `finish_reason: "error"`（我造的旧日志
  原文：`{'content':'HTTP 500: x','finish_reason':'error','request_id':…,'tool_calls':[],'usage':{}}`），
  所以任何 M0–M2 期间失败过的日志都无法再装回 `ModelResponse`。当前没有调用方（
  `grep -rn RecordedModel src/` 只有定义与 `__all__`），所以是潜在坑而不是现行故障。
- 建议：`from_payload(..., on_unknown="raise"|"map")` 或直接把 docstring/capability 说清"只保证读新日志"。

> 注意：题面里"`__post_init__` 强制 usage 有两个键，老事件 `{}` 会不会炸"这个前提**不成立**：
> `__post_init__` 是**补齐**（`normalize_usage` 返回两个 `None`），不是**校验**。
> 实测 `ModelResponse.from_payload({"finish_reason":"end_turn","usage":{}})` →
> `usage={'input_tokens': None, 'output_tokens': None}`，不抛。

### S5. `to_anthropic_messages` 有 4 个 Anthropic 硬约束没消化，会把 400 留给线上

- 位置：`src/tianshu/model.py:548-603`
- 逐个实测（`to_anthropic_messages(...)` 直接调用）：

  | 历史 | 现在产出 | Anthropic 的反应 |
  |---|---|---|
  | 只有 `system`，没有 `user` | `('s', [])` → 请求体 `messages: []` | 400（至少要一条消息） |
  | 首条是 `assistant` | `roles == ['assistant']` | 400（首条必须是 user） |
  | `assistant` 的 `content=""` 且无 `tool_calls` | `{"type":"text","text":""}`（`model.py:601`） | 400（空 text 块） |
  | `tool` 消息的 `tool_call_id` 在历史里找不到对应 `tool_use`（实测传 `call_ghost`） | 原样发出 `tool_use_id: 'call_ghost'` | 400（unexpected tool_use_id） |
  | `arguments` 是合法 JSON 但**不是对象**（实测 `'[1,2]'` / `'5'` / `'"s"'`） | 静默变 `input: {}`，且不留痕 | 不 400，但信息丢失（`model.py:591`） |

  前四条目前都**不能从主循环触发**（`initial_messages` 永远有 user；空 assistant 会直接终结 Run），
  但 adapter 是接缝的公共面，而且 resume/裁剪历史后前两条是可能出现的。
  建议：出站前做一次结构自检，不满足就抛 `ModelError(kind="protocol")`——在本地报错比在 400 里猜便宜。

### S6. 重复 `tool_use` id：主循环明确承认会发生，adapter 不做处理

- 位置：`src/tianshu/loop.py:666`（"provider 每轮从 `call_0` 重新编号是常见的"）、
  `src/tianshu/model.py:583-596`（历史重排时原样复用 id）
- 复现：构造两轮 assistant 各带 `id=call_0` 的历史，`to_anthropic_messages` 输出两条
  `({'assistant': ['call_0']}, {'user': ['call_0']})` 的重复 id。
  Anthropic 是否按 request 去重会 400，**我没有真机验证**；如果会，那么"每轮复用 call_0 的
  Anthropic 兼容代理"上第二天就会挂。建议真机确认，必要时**只在出站时**重新编号（不动内部形状，
  否则 `messages_hash` 会变，违反 PRD rule 1）。

### S7. 契约测试与真实 C2 投影脱钩（B1 漏网的直接原因）

- 位置：`tests/test_model_contract.py:119-129`（手写 `TOOLS`）、`:172-179`（只断言工具名）
- 建议补一条：用 `tool_manifest()` 装配，断言 OpenAI body 里 `tools[0]["function"]["parameters"]`
  是字典、Anthropic body 里 `tools[0]["input_schema"]` 是字典。这条测试现在写会红（B1），修完会绿，
  正好是 M3 判据该有的样子。
- 顺带：`tests/test_model_contract.py:363-375` 的 `var` 参数化没被用到（函数体里自己 delenv 了两个变量），
  实际只把同样的断言跑了两遍——不是 bug，但会让人误以为覆盖了"只缺一个 key"的情形。

### S8. 提交的文档引用了未提交的文件（合并后即死链）

- `docs/TECH-ARCH.md:459` 与 `docs/superpowers/plans/2026-11-09-m3-swappable.md:92` 都指向
  `docs/superpowers/notes/m3-backend-seam-proposal.md`，但 `git ls-files docs/superpowers/notes/` 为 0
  ——该目录在 `git status` 里是 untracked。合并到 `main` 后这两个引用都会 404。
- 建议：要么把那份 notes 一起提交，要么把引用改成"（M3 Task 2 落地后补）"。

### S9. Anthropic 复用 `TIANSHU_BASE_URL` / 回落 `TIANSHU_API_KEY`

- 位置：`src/tianshu/model.py:446`（`base_url or os.environ["TIANSHU_BASE_URL"] or "https://api.anthropic.com/v1"`）、
  `model.py:448-453`（`ANTHROPIC_API_KEY` 回落 `TIANSHU_API_KEY`）
- 失败形态：一个只配了 openai 那两个变量的仓库，加 `--provider anthropic` 后请求会打到
  `https://api.openai.com/v1/messages` → 404，报错是 `HTTP 404 …`，看不出是 base_url 串了。
- 建议：优先读 `ANTHROPIC_BASE_URL`，或至少在 `README.md:130-136` 写明这两个变量是两个 provider 共享的。

### S10. `kind="auth"` 被用来表示"命令行缺参数"

- 位置：`src/tianshu/cli.py:199-203`（缺 `--model-name` 时抛 `ModelError(kind="auth", retryable=False)`）
- `kind` 是封闭集，语义是"鉴权/传输/协议"。把"用户少写了命令行参数"塞进 `auth`，会让将来按 kind 分流
  的逻辑把配置错误当鉴权错误（比如提示"请检查 key"）。建议改抛 `TianshuError`；`main()`
  （`cli.py:872-874`）本来就 `except Exception` 打印可读错误，行为不变。

### S11. 13 值里 `max_tokens` 与 `limit_output_tokens` 同义，且 `max_tokens` 永不产生（低）

- `docs/PRD-v1.md:436-444` 同时列了两个；`src/tianshu/model.py:525`（OpenAI `length`）与
  `model.py:532`（Anthropic `max_tokens`）都映射到 `limit_output_tokens`。建议在 PRD 注明
  `max_tokens` 留给谁，避免下一个人以为该产生它（这是 PRD 旧账，不算本 PR 引入）。

---

## 3. 已核查无问题

### N1. "主循环 0 改动"成立（PRD §11）

```bash
git -C /Volumes/Date/天枢Agent diff main -- src/tianshu/loop.py   # 输出为空
git -C /Volumes/Date/天枢Agent diff --name-only main...HEAD -- src/  # src/tianshu/cli.py  src/tianshu/model.py
```

`ModelError` 能被旧主循环接住，靠的是 `loop.py:612` 已有的 `except Exception`——
这一点我实跑验证过（事件序列为 `… model.intent, model.error, job.finished, run.end …`），不是靠推断。

### N2. 基线

```bash
uv run --offline pytest -o addopts="" -q -p no:cacheprovider   # 401 passed in 24.73s
uv run --offline ruff check .                                   # All checks passed!
```

### N3. 题面点名要验的三处退化，测试都会红（我逐个真改过，改完已还原）

做法：把 `src/tianshu` 拷到 `/tmp/rv15/src`，打补丁后用 `PYTHONPATH=/tmp/rv15/src` + `-o pythonpath=`
跑测试（已确认 `tianshu.model.__file__` 指向副本）。

| 变异 | 结果 |
|---|---|
| `_map_finish` 兜底改回 `return "end_turn"` | **RED**：8 failed（`test_unknown_finish_reason_is_error_not_success` ×6、`test_missing_finish_reason_is_error` ×2），外加 `test_debug_regressions.py` 里那条"错误不是 noop"也红 |
| `retryable` 漏掉 5xx（删 `status_code >= 500`） | **RED**：4 failed（500/503 两个 vendor 各两条） |
| `from_http` 把 429 归成 `http_status` | **RED**：3 failed |
| `system` 留在 messages 里（不 `continue`，推成 user 文本） | **RED**：2 failed（`test_anthropic_url_and_headers` 的 `system` 顶层 + `roles == ["user"]`） |
| 去掉 `finish_reason` 封闭集校验 | **RED**：5 failed |
| 去掉 `usage` 归一化 | **RED**：1 failed |
| 不合并相邻同角色消息 | **RED**：1 failed |

### N4. 回放与 `messages_hash` 没有被这次改动打断（这是我最担心的一条，做了跨版本实验）

1. 用 `git archive main` 导出旧代码 → 旧代码写一个含 `finish_reason:"error"` 的 Run（`/tmp/old/e.db`）；
   **用本分支的新代码回放它**：
   ```
   run_id : run_8a8156d2b40e
   事件数 : 13    轮数 : 1    校验 : 1 次 messages_hash
   结论   : 通过
   ```
   → 旧日志仍然可回放，`usage` 形状变化不影响（`replay.py:88-105` 只读 `content` / `tool_calls`）。
2. 新格式实跑（scripted，`--db /tmp/rv15/e.db`）：`replay : 通过（33 事件 / 3 次消息校验）`。
3. 一致性链条本身没被动：`loop.py:721` 写 `resp.to_payload()`；`_assistant_message`（`loop.py:227-241`）与
   `replay.py:93-104` 对 `arguments` 都用 `json.dumps(..., ensure_ascii=False, sort_keys=True)`，
   半截 JSON（字符串）在两边同样被二次编码，因此仍然逐字一致。

### N5. `Usage` 未知为 `null` 不是 `0`，且缓存 token 不并入

`src/tianshu/model.py:190-207`；`tests/test_usage.py` 全绿（含 `True` 不算 `1`、部分缺失保 `None`）；
`tests/test_model_contract.py:213-227` 覆盖 Anthropic 缓存 token 不被折进 `input_tokens`。与
`docs/PRD-v1.md:342-344` 一致。

### N6. `finish_reason` 13 值与 PRD §5.5 逐字一致，且没有与 `run_stop_reason` 混用

`model.py:38-55` 的集合与 `docs/PRD-v1.md:437-444` 的 `Literal` 完全相同（13 = 13，我逐项比对）；
`grep -rn "run_stop_reason" src/` 为空 → 本 PR 没有引入"把运行侧原因塞进模型侧"的混用。

### N7. `ModelError` 与 PRD 口径一致

`kind` 六值与 `docs/PRD-v1.md:338` 相同（`model.py:74`）；`from_http` 分类（401/403→auth、429→rate_limit、
408→timeout、其余→http_status）与 `__repr__` 带 kind/status 都有测试
（`tests/test_model_contract.py:260-288`、`:402-407`），`repr` 确实被 `loop.py:613-615` 写进事件。

### N8. `--model-name` 默认值变化向后兼容

- `src/tianshu/cli.py:204`：openai 分支回落 `"gpt-4o-mini"`，旧命令行行为不变（`tests/test_model_contract.py:510-517` 断言）。
- `args.model_name` 全仓唯一消费者是 `_model_from`（`cli.py:195`），不存在把 `None` 漏进别处的路径；
  `run` 与 `worker` 两个子命令都加了 `--provider`（`cli.py:76`、`:107`）。
- `README.md:117-136` 已改成实际行为；`grep -rn "model-name" docs/ README.md` 没有残留旧默认值。

### N9. 零运行时依赖

`pyproject.toml` 的 `dependencies = []` 未变；新增代码只用 `json` / `urllib.request` / `urllib.error` / `os`。
`ruff check .` 干净（`I` 规则也开着）。

### N10. Anthropic 侧的协议必需项是对的

`src/tianshu/model.py:460-477`：`max_tokens` 必填已给（`:464`）、`system` 提到顶层（`:473-474`）、
`tools` 直接吃 C2 投影且字段名 `input_schema` 正是 Anthropic 原生（`:472`）、
`x-api-key` + `anthropic-version` 头齐（`:477`、`:441`）。测试断言在
`tests/test_model_contract.py:191-202`。

---

## 4. 题面六个必答问题的直接结论

1. **会不会打断真实 provider？** 不会把"改前成功"的调用变成失败——旧兜底是 `"unknown"`（我实测
   `_map_finish(None/''/weird)` 全返回 `"unknown"`），null / 未知值在改前也是 Run failed。
   所以 `_map_finish` 这一处是"更诚实"，不是"更容易误伤"。**真正会打断真实 provider 的是 B1**：
   OpenAI 侧工具参数 schema 根本没交给模型。另外未映射会把那轮 content/usage 丢掉（S1），
   `pause_turn` 被自己的文档说成已覆盖却实际报错（S2）。
2. **`usage` 归一化会不会破坏旧日志/回放？** 不会。前提本身不成立：`__post_init__` 是补齐不是校验，
   `usage: {}` → 两个 `None`，`from_payload` 不抛（实测）。`replay` 完全不构造 `ModelResponse`，
   我用 `main` 写出的旧日志实测回放通过（13 事件 / 1 次 hash 校验）。唯一真的破的是
   `from_payload` 读旧日志里的 `"error"`/`"unknown"`（S4），当前无调用方。
3. **`to_anthropic_messages` 的边界会不会 400？** 只有 system、assistant 开头、空 text 块、
   幽灵 `tool_use_id` 这四种**现在能从 adapter 发出会被 Anthropic 拒绝的请求**，但都不可从当前主循环触发
   （S5）；重复 `tool_use` id 是**可能被主循环触发**的（`loop.py:666` 自己承认 provider 会复用 `call_0`），
   是否 400 需真机确认（S6）。`arguments` 为数组/数字不会 400（都会变 `{}`），但静默丢信息，没有测试覆盖这一分支
   ——现有测试只覆盖了"半截 JSON"（S5 表格最后一行）。
4. **文档与代码一致吗？** 三条规则里 rule 1（双向翻译）**在 OpenAI 侧没做到**（B1）；rule 2（不许修好半截 JSON）
   做到了（`model.py:405-412` 保留原串、`loop.py:203-214` 据此拒绝派发），但出站方向把非对象参数静默变 `{}`
   属于同一层语义的空白（S5 表格最后一行）；rule 3（新语义走 `docs(contract)`）本 PR 自己守住了
   （`docs/PRD-v1.md` 的改动在 `d353dc4 docs(contract):` 提交里）。`ModelError` 口径四条代码全做到。
   另有 `pause_turn` 的 docstring/实现自相矛盾（S2）。
5. **测试能不能发现退化？** 能。点名的三处（兜底改 `end_turn`、`retryable` 写错、`system` 留在 messages）
   我都真改过，全部变红（N3，7 项变异 7 红）。但**它测的是手的形状而不是真实投影**：契约测试从不调用
   `tool_manifest()`，所以 B1 这种"adapter 与 C2 真形状不匹配"的问题恰好漏网（S7）。
6. **CLI 默认值变化会不会破坏已有用法？** 不会。openai 分支回落 `gpt-4o-mini`、`model_name` 只有一个消费者、
   `run`/`worker` 都同步、README 已更新（N8）。命名债（`--model openai-compatible` + `--provider anthropic`
   读起来矛盾）和共享环境变量（S9）值得记一笔，但不构成兼容性破坏。

---

## 5. 诚实边界：我没能验证的东西

- **真实端点**：本机没有 key、也没有发起外网调用，所以计划里那张"两个协议各跑一次完整 Run"的表格
  （run_id / 轮数 / tokens / replay 通过）**没有被独立复现**。我的回放实验用的是 `main` 代码 +
  本地 fake Transport，证明的是"代码路径与跨版本兼容"，不是"线上端点可用"。
- **OpenAI 对 `input_schema` 的确切反应**（400 还是忽略）：未在真机验证。但"请求体里没有 `parameters`"
  是确定的事实，两种反应都不是正确翻译。
- **Anthropic 是否拒绝跨消息重复 `tool_use` id**：未验证（S6）。
- 我是在 `/tmp` 副本上做变异测试的：`/tmp/rv15/src`（新代码副本）、`/tmp/old`（`git archive main`）。
  仓库的 `src/` / `tests/` / 文档内容未被改动，只新增了本文件。
