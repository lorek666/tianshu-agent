# SPEC · 真 embedding provider 与千条级检索评测（W6）

> 日期：2026-11-14 · 分支：`feat/embedding-provider` · 基线：`main@c2cf1c7`
> 路线图：ROADMAP W6（账本文件随 PR #151 交付）。
> 性质：**产品行为变更（非契约变更）**——C1–C5、事件 payload、工具 schema 均不动；
> 新增的是「用户显式配置才有」的外部 embedding provider 与它的**外发边界**。
> PRD §1351「embedding 只有 HashEmbeddingProvider」与 §1397「真 embedding provider
> 仍不成立」两处在实现后对账。
> 冲突面：`memory_index.py` / `memory.py` / `loop.py` 装配段 / `scripts/eval_memory_*.py`
> 均不在任何在途 PR（#140/#141/#146/#150/#151/#152）的文件清单里。

## 1. 现状（代码事实）

- 接缝完整：`EmbeddingProvider` 协议（`model_id` / `dim` / `embed()`）+
  `HashEmbeddingProvider` 确定性哈希基线 + `MemoryIndex`（RRF 融合、
  `model_id+dim` 过滤、`availability()` 三条校验）——`memory_index.py`。
- 装配只认 hash：`loop.py:_embedding_provider_from_env()` 对其它名字直接报错
  「外部 provider 需要数据外发许可，尚未接线」；`build_stack(embedding_provider=)`
  注入点已在；`tianshu memory rebuild-vectors` 是唯一的显式构建入口（CLI）。
- `retrieve()` 三个调用点：`loop.py:2288`（模型工具 `memory.search`）、
  `memory_control.py:147`（面板 RPC）、`cli.py:1578`（CLI search）。
  CLI 与主循环**共用同一个投影实例**——不能靠实例区分外发面，gate 必须落
  在调用点上。
- 评测现状：`scripts/eval_memory_retrieval.py` 22 query / 25 记忆，
  Recall@5 两路都 1.000（饱和）——小样例证明不了「千条规模下两路还行不行」。
- `MemoryIndex.__init__` 在构造时就读 `provider.model_id` / `provider.dim`
  ——**dim 必须在构造 provider 时已知**，不支持"探测式" provider。

## 2. 设计

### 2.1 OpenAICompatibleEmbeddingProvider（新模块 `src/tianshu/embedding_openai.py`）

- `http.client` 直连（零第三方依赖）：POST `<base_url>/embeddings`，
  body `{"model": ..., "input": [...]}`；TLS 校验恒开、无重定向跟随
  （端点是用户显式配置的 origin，不跟随 30x）、整请求超时、响应按字节上限
  读取。**不走 `net.fetch`**：那是工具层 GET 执行器（canonical URL + 审批表），
  这里是库层显式配置动作，两者语义不同。
- 批量分片：`embed()` 内部按每批 ≤ 64 条切分，逐批请求，失败即停。
- 错误分类：`EmbeddingError(reason)`，`reason` ∈ {`auth`, `quota`, `status`,
  `network`, `timeout`, `shape`}——封闭集，供 CLI 如实报错；不做自动重试
  （rebuild 是离线显式动作，失败了人重跑；一次失败半途的索引保持旧状态，
  `rebuild()` 的事务边界不变）。
- 配置四件套，**只从环境变量读**（真实 key 永不落盘、不进事件）：
  `TIANSHU_EMBEDDING_PROVIDER=openai` + `TIANSHU_EMBEDDING_BASE_URL` +
  `TIANSHU_EMBEDDING_MODEL` + `TIANSHU_EMBEDDING_DIM` +
  `TIANSHU_EMBEDDING_API_KEY`。缺任一项在装配时报 `TianshuError`，文案点名
  缺什么——不静默降级成「没有 provider」（沿用 `_embedding_provider_from_env`
  未知名字报错的既有口径）。
- `dim` 显式配置（§1 的构造时序决定）：响应向量实际维度 ≠ 配置 dim →
  `EmbeddingError(shape)`，**不写入任何向量**（混空间行比降级更糟）。
- `model_id = f"openai:{model}"`：进 `memory_vector_meta`，换模型即换空间，
  旧向量不参与融合直到 `rebuild_vectors()` 重算（既有机制，零新增）。
- `sends_data_off_device = True`（见 §2.3）；`HashEmbeddingProvider` 补
  `= False`。`retrieve()` 的 gate 用 `getattr(provider, "sends_data_off_device",
  True)` 读——未知 provider 按会外发处理（安全默认）。

### 2.2 装配

- `_embedding_provider_from_env()` 增 `openai` 分支：读四件套 → 实例化；
  其余行为不变（空 → None、hash → 基线、未知 → 报错）。
- `build_stack(embedding_provider=)` 显式注入优先，不变。

### 2.3 外发边界（本 SPEC 的核心论证）

这次接线把**两种新的数据外发**带进了系统，边界必须先于代码说清：

| 动作 | 外发内容 | 谁触发 | gate |
|---|---|---|---|
| `rebuild-vectors` | **全部**记忆正文 | 人敲 CLI 命令（唯一入口） | 显式 env 配置 + 显式命令 = 双显式 |
| 控制面检索（CLI search / 面板 RPC） | **query 文本** | 人在终端/UI 敲检索词 | 同上 |
| **模型工具 `memory.search`** | （若无 gate：query 文本） | 模型决定 | **`retrieve()` 默认拒绝：不外发** |

- 实现：`MemoryProjection.retrieve(..., allow_embedding_outbound: bool = False)`
  ——默认 **False**（安全默认，靠调用点显式打开）。`False` 且 provider
  `sends_data_off_device=True` → 不调 `index.search()`，走既有降级分支，
  `degraded` 原因写明「外部 embedding provider 的网络外发未被本次调用允许」。
- 三个调用点：模型工具**不传**（恒 False）；面板 RPC 与 CLI search 传 True
  （人显式动作，与 rebuild 同级）。
- 一句话边界：**外部 embedding 的网络调用只发生在人显式发起的控制面动作里；
  模型 Run 的检索永不触发外发**——这条是执行侧结构保证（调用点默认值），
  不是提示词、不是黑名单。模型想要语义检索 → 将来把 embedding origin 纳入
  `outbound_allow` 体系（W9 评估，本 SPEC 不做）。
- 与红线 4 的关系：授权（这里是外发许可）只在执行侧判定；模型工具路径的
  gate 不读模型输出、不接受工具参数控制，判据用端点请求计数钉死（§3 #6）。

### 2.4 千条级评测（新 `scripts/eval_memory_scale.py` + `docs/memory-scale-eval.md`）

- 动机：现有 22/25 样例 Recall@5 饱和，回答不了「规模上去以后 FTS 与混合
  检索还行不行、RRF 的 k 敏不敏感」。
- 方法：**确定性合成** N 条背景记忆（固定 seed、模板生成、内容去重检查）+
  固定黄金 query 集（复用现有样例的期望条目风格）+ N ∈ {50, 200, 1000} +
  RRF k ∈ {30, 60, 100}；指标 Recall@5 / MRR。
- 两路对照：FTS-only vs hybrid-hash（哈希基线**没有语义能力**——报告沿用
  既有措辞，不宣称语义召回；它的价值是「两路名次融合在规模下的行为」）。
- 真 provider 分数**留接口**（`--provider openai` + env 四件套，跑起来同一套
  判据）；没真端点凭据时报告如实写「未跑真 provider」，不编数字。
- 现有 `eval_memory_retrieval.py` **不动**（其数字已被文档引用，保持可比）。

## 3. 判据（新增 `tests/test_embedding_provider.py`、`tests/test_memory_outbound_gate.py`）

| # | 测试 | 判据 |
|---|---|---|
| 1 | provider 正常路径 | httpserver 夹具返回合法响应 → `embed()` 返回向量、批内多次请求被正确分片 |
| 2 | dim 不符即拒 | 响应维度 ≠ 配置 dim → `EmbeddingError(shape)`，且**不产出向量** |
| 3 | HTTP 错误分类 | 401→`auth`、402/429→`quota`、500→`status`、断连→`network`、慢→`timeout` |
| 4 | 装配四件套 | env 齐 → 实例化成功、model_id 正确；缺任一 → 报错点名缺什么；未知 name → 既有报错不变 |
| 5 | hash 不受 gate 影响 | 模型工具路径 + hash provider → 语义路照常（hash 本机计算，不外发） |
| 6 | **模型检索零外发** | 外部 provider + 可用索引 + 模型工具路径 → `semantic=False`、`degraded` 写明外发不允许、**端点请求计数 = 0** |
| 7 | 控制面显式外发 | 同配置 CLI search → 语义路真的跑、端点恰好收到 `ceil(N/64)` 次请求 |
| 8 | rebuild 即外发（显式） | `rebuild-vectors`（CLI）→ 全部正文发送、meta 写入 openai model_id、成功后 `availability()=True` |
| 9 | 评测脚本小规模跑通 | fake provider 注入 + N=50 → `--json` 输出各 N×k 组合的 Recall/MRR，数字与手算一致 |
| 10 | 既有全绿 | 全量门禁不变（conftest 已把限速钉 0；embedding 测试全部走 loopback 夹具） |

## 4. 负控（变异后必须红，逐条实跑记录）

- **NC1**：拆外发 gate（`allow_embedding_outbound` 判定恒 True）→ 判据 #6 红
  （端点计数 0 → >0）。这条钉 §2.3 的边界本体。
- **NC2**：拆 dim 校验（维度不符也照存）→ 判据 #2 红。
- **NC3**：评测脚本漏跑 hybrid 路仍输出 hybrid 数字 → 判据 #9 红
  （数字与固定 seed 预期不符 / 字段缺失）。

## 5. 文档对账

- PRD §1351（「embedding 只有 Hash…」）与 §1397（「真 embedding provider
  仍不成立」）：改为已交付 + 新边界（模型检索不外发、控制面显式外发）。
- KNOWLEDGE §7 N2 行划线更新 + §8 会话日志。
- `memory_index.py` docstring：协议描述补 `sends_data_off_device`。
- 新 `docs/memory-scale-eval.md`：评测方法 + 实测数字 + 「不是语义能力证明」。
- 本 SPEC：判据/负控实测回填。

## 6. 边界与未决

- 模型 Run 的语义检索（把 embedding origin 纳入 `outbound_allow`）不做——
  W9 评估；当前模型侧恒 FTS-only（配置了外部 provider 也是，如实降级）。
- 真 provider 的**实测检索分数**没有（需要真实端点凭据）——评测报告只给
  哈希基线数字 + 真 provider 接口，不宣称语义收益。
- reranker、跨作用域融合调优：未决，另立工单。
- 不接 net.py 的速率限制/重试（那是工具层；rebuild 是离线显式动作，失败人重跑）。
- 代理/TLS 定制/批量 embedding 缓存：不做（显式配置端点足矣，缓存属于索引
  一致性话题）。
- Windows：`http.client` 标准库跨平台；测试全走 loopback，CI 会实测。

## 7. 高危判定

不碰审批/派发/事件面；新增外发面被「显式 env 四件套 + 显式控制面命令」双门
收住，模型路径默认 False 是执行侧结构保证。不构成强制独立复审项；但判据 #6
（零网络调用）是红线 4 的正面延伸，测试与负控 NC1 都钉在它上。

## 8. 实测回填（2026-11-14，macOS arm64 本机）

- `tests/test_embedding_provider.py` **13 passed**（判据 #1–#4 与封闭集/
  base_url 校验/timeout env）；`tests/test_memory_outbound_gate.py`
  **6 passed**（#5–#8 + 回滚 + 安全默认 + 评测脚本判据 #9）。记忆域全文件
  **252 passed**。closed 端口在本机表现为连接超时（与 W2 同一平台差异），
  network/timeout 断言按集合口径写。
- 负控三条实跑红还原：**NC1** 拆外发 gate（判定恒 True）→ 零外发/安全默认
  2 红；**NC2** 拆 dim 校验 → 维度不符测试 1 红（变异把 `if` 块拆坏过一次，
  语法错误不是有效负控，修正为 `pass` 后红）；**NC3** 拆脚本 hybrid 的
  rebuild → 评测判据 1 红（MRR 差异消失即红——固定 seed 的 0.767 vs 0.975）。
- 千条级评测（`--json` 实跑）：N=1000 FTS-only Recall@5 **0.650**
  （lexical 0.4 / mixed 0.9），hybrid-hash **1.000**；k∈{30,60,100} MRR
  逐位相同（不敏感）。完整表与读法见 [memory-scale-eval.md](memory-scale-eval.md)。
- 评测脚本固定 seed 的期望值进了判据测试（`_run_mode(50)` 的 MRR 差异 +
  两次跑逐位一致）——脚本数字漂移会当场红。
- 全量与 CI 绑定对应 PR SHA，不在本文重复记录。
