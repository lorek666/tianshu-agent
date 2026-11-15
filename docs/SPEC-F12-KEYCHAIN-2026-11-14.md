# SPEC · F12 macOS Keychain 凭据后端（W1）

> 日期：2026-11-14 · 分支：`feat/credentials-keychain` · 基线：`main@c2cf1c7`
> 路线图：[ROADMAP-COMPLETE-AGENT-2026-11-14](ROADMAP-COMPLETE-AGENT-2026-11-14.md) W1
> 性质：**产品能力交付（非契约变更）**——C1–C5 不动；`credentials.py` 的三态
> 语义与接缝保持不变，只是把「unsupported」变成「macOS 上真的能用」。
> 冲突面：`credentials.py` 自 e72982d（F12 接缝）后无人再改；在途 PR #140/#141/#146
> 均不触碰 `src/tianshu/credentials.py` 与 `tests/test_credentials.py`。

## 1. 现状（代码事实 + 本机探针证据）

- 接缝完整：`CredentialStore` 协议（get→`Secret|None` / set / delete）、
  三态语义（unsupported ≠ 未配置 ≠ 可用）、`Secret` 打码、`default_store()`
  按 `TIANSHU_CREDENTIAL_BACKEND` 选择、`KeychainCredentialStore` 是一请求就
  报 `CredentialUnsupported` 的诚实 stub（`implemented=False`）。
- 模块 docstring 拒绝实现 Keychain 的理由是「我们没有任何平台能测它」——
  **这个前提在 macOS arm64 本机上已不成立**。2026-11-14 探针（见 §2）确认
  `SecItemAdd/CopyMatching/Update/Delete` 对默认 keychain 全链路可用。
- 生产取值路径：`model._credential_for()` → `default_store().get()`；
  `None` → 空串走「缺少 API key」，`CredentialUnsupported` 直接抛、不回退 env。

## 2. 本机探针结论（2026-11-14，darwin 25 / macOS arm64，逐条实测）

1. `SecKeychainCreate` 一律 `-25294`（errSecNoSuchKeychain）：**文件 keychain
   的进程内创建已不可用**（`.keychain` / `.keychain-db` / 无扩展名三种路径都试过）。
2. `SecItemAdd` 挂 `kSecUseKeychain=<文件引用>` 时**该键被忽略**：条目实际落进
   login keychain（`security dump-keychain <文件>` 0 命中、
   `security find-generic-password` 命中 login）——文件 keychain 的 SecItem
   路由在这版 macOS 上已失效。
3. **默认 keychain 路径全链路可用**：add → duplicate add `-25299` →
   `SecItemCopyMatching` 读回 → `SecItemUpdate` 改值 → `SecItemDelete` →
   再读 `-25300`（item not found），全部实测。
4. `security create-keychain`（OS 自带 CLI）可创建临时 keychain 文件且不污染
   搜索列表——但它救不了第 2 条（SecItem 不路由到文件），只作历史证据。
5. 探针自身污染已清理：探针条目（service `tianshu-agent-probe` /
   `tianshu-agent-kc5`）已从 login keychain 删除（`find-generic-password`
   复核 0 命中）。

**复审更正（2026-11-15，独立第二读者两臂对照探针）**：第 2 条「kSecUseKeychain
被忽略」独立复现**失败**——同一台机器上，挂 `kSecUseKeychain=<文件引用>` 的
SecItemAdd rc=0 且条目**落进文件 keychain**（dump-keychain 命中、login 0 命中），
对照臂（无该键）落 login。两轮探针矛盾 ⇒ 平台行为不可依赖：「只支持默认
keychain」改为**范围裁决**，不以任一轮探针事实为依据（见 §6）。

**设计随之改定**：生产与测试都走**默认（login）keychain**，不做文件 keychain。

## 3. 设计

### 3.1 新模块 `keychain_macos.py`（ctypes 直调，非 darwin 不加载）

- 延迟加载：`credentials.py` 仅在 darwin 分支 `import keychain_macos`；
  模块顶层不 import ctypes 之外的平台库（零第三方运行时依赖不变，
  ctypes/util 是标准库）。
- 绑定面（全部实测过签名）：`SecItemAdd`、
  `SecItemCopyMatching`、`SecItemUpdate`、`SecItemDelete` + CoreFoundation 的
  `CFStringCreateWithCString` / `CFDataCreate` / `CFDataGetLength` /
  `CFDataGetBytes`（**CFRange 是结构体**——探针里传成单个 long 会 CF 断言崩溃）
  / `CFDictionaryCreateMutable` / `CFDictionarySetValue` / `CFRelease`。
- 常量从框架 `in_dll` 读取（`kSecClass` / `kSecAttrService` / `kSecAttrAccount`
  / `kSecValueData` / `kSecReturnData` / `kSecMatchLimit(One)` / `kSecUseKeychain`
  / `kSecClassGenericPassword` / `kCFBooleanTrue`），不手抄数值。
- **防交互卡死**：查询统一挂 `kSecUseAuthenticationUI = kSecUseAuthenticationUIFail`
  ——daemon 是无头进程，钥匙串锁定时必须**立即失败**（`-25308`
  errSecInteractionNotAllowed → `CredentialUnsupported("钥匙串已锁定/需要交互解锁")`），
  绝不弹 GUI 密码框。
- 域命名：`kSecAttrService = "tianshu-agent"`、
  `kSecAttrAccount = f"{provider}/{capability_class}"`（与 `CredentialRef.__str__`
  同形，env 后端的分域语义原样映射）。
- OSStatus 映射：`0` 成功；`-25300` item not found → `get()` 返回 `None`
  （**未配置**不是错误）；`-25299` duplicate → `set()` 走 `SecItemUpdate`；
  其余（含 `-25308`）→ `CredentialUnsupported`（带 OSStatus 数值与含义）。

### 3.2 `credentials.py` 接线

- `KeychainCredentialStore(keychain_path=None)`：`keychain_path` 参数**保留但
  在本版只支持 None**（默认 keychain）——传路径直接 `ValueError`，因为探针
  证明文件路由已失效，提供一个"看起来能用其实不路由"的参数就是撒谎
  （模块的一贯立场）。非 darwin：行为与现状一致（三方法全 `CredentialUnsupported`，
  `implemented=False`），错误文案更新为「仅 macOS 实现」。
- 能力声明分平台：darwin `implemented=True, supports_write=True,
  secure_storage=True`（新常量 `KEYCHAIN_CAPABILITIES_MACOS`）；非 darwin 沿用
  `KEYCHAIN_CAPABILITIES`（不变）。
- `set()`：add → `-25299` 时 update（读-改-写窗口内条目始终存在，无「删了还没写回」
  的空窗）。`delete()`：`-25300`（本就不存在）按幂等成功处理——终态「不存在」
  已达成，不算假装成功；其余失败照抛。
- `default_store()`：`TIANSHU_CREDENTIAL_BACKEND=keychain` 在 darwin 返回真实现；
  非 darwin 返回 stub（行为不变）。无静默降级：darwin 上 `get()` 返回 `None`
  就是「没配」，不会偷偷去读 `TIANSHU_API_KEY`。
- 取值即登记：`get()` 拿到明文后 `register_secret()`（与 env 后端一致）。

### 3.3 与既有测试的关系（有意变更，逐条说明）

| 既有测试 | 处置 |
|---|---|
| `test_keychain_is_declared_unimplemented` | 平台条件化：非 darwin 保留；darwin 改断言 `KEYCHAIN_CAPABILITIES_MACOS` |
| `test_keychain_raises_unsupported_everywhere` | 平台条件化（非 darwin）；darwin 换成真后端行为测试 |
| `test_keychain_does_not_silently_fall_back_to_env` | **契约级测试必须存活**：darwin 上改用「空 keychain（无条目）」构造模型——构造成功但 `complete()` 必须报「缺少 API key」，且请求不得带 `Bearer <env 里的假 key>`；非 darwin 原样保留 |
| `test_cli_reads_key_through_the_store_not_os_environ` | 同上拆平台：darwin 用空 keychain + 假 transport，断言请求不带 env 假 key |
| 其余 env / 脱敏 / 事件库测试 | 不变 |

### 3.4 测试要碰真实钥匙串——显式偏差（如实登记）

「测试必须使用临时数据根」的纪律**在本工作流无法完全遵守**：真实 SecItem
行为测不出替身。处置（复审轮已把实现补齐到本节承诺——初版实现只做了
account 级唯一，三个测试甚至直删生产槽，属实现偏离 SPEC）：

- **service 级命名空间隔离**：全部 keychain 测试经 autouse 夹具把
  `credentials._KEYCHAIN_SERVICE` patch 成固定测试 service
  `tianshu-agent-selftest`，与生产命名空间（`tianshu-agent`）结构性互不相撞；
  account 名再叠 `selftest-<uuid4hex>` 每跑唯一；
- **生产槽只读守卫**：同一夹具对生产 `openai/llm` 槽位做进入/退出双读比对，
  任何测试绕过隔离直写生产槽即红；守卫消息只报「有值/空」，不泄载荷；
  牙齿经变异验证（种哨兵值 → 直删生产槽 → 守卫红 → 还原）；
- `finally` 强制清理 + 复核清理成功（find → `-25300`）；清理失败要红，
  不静默留残留；
- 钥匙串锁定/不可用（`-25308`）→ `pytest.skip(reason)`（跳过原因写明），
  不假绿；
- CI macOS runner 与本机都能跑；ubuntu CI 上这些用例按平台跳过（skip 原因）。

## 4. 判据（新增测试，全部落在 `tests/test_credentials.py`）

| # | 测试 | 判据 |
|---|---|---|
| 1 | roundtrip | set → get 返回 `Secret` 且值相等；repr 打码；`register_secret` 生效（redact 能抹） |
| 2 | absent is None | 未写入的域 get → `None`，不是空串、不是异常 |
| 3 | overwrite | set 同一域两次 → get 返回第二次的值（update 路径） |
| 4 | delete | delete 后 get → None；delete 不存在的域 → 幂等成功 |
| 5 | 域隔离 | `("openai","llm")` 与 `("anthropic","llm")` 互不可见 |
| 6 | 防回退 | 空 keychain + env 假 key：模型构造成功、`complete()` 报缺 key、请求头不带假 key |
| 7 | 能力声明 | darwin caps: implemented/supports_write/secure_storage 全 True；非 darwin 维持 stub 断言 |
| 8 | service 命名 | 写入后 `security find-generic-password -s <service>` 能找到、清理复核 `-25300`（外部观察者视角） |
| 9 | `default_store()` | darwin + backend=keychain → 真实现实例；非 darwin → stub |
| 10 | 既有 24 条 | 全绿不变 |

## 5. 负控（变异后必须红，逐条实跑记录）

- **NC1**：`get()` 把 `-25300` 吞成返回 `Secret("")` → 判据 2 红（三态语义塌掉）。
- **NC2**：`set()` 去掉 duplicate→update 分支 → 判据 3 红。
- **NC3**：darwin 分支静默回退 env（把 `CredentialUnsupported` 换成读
  `TIANSHU_API_KEY`）→ 判据 6 红（防回退是本模块的立身之本）。

## 6. 边界与未决（如实）

- **平台**：只交付 macOS；Windows Credential Manager / Linux Secret Service
  仍 `unsupported`，错误文案写明。不宣称 Windows 实机验证。
- **跨二进制访问**：同一解释器路径写入后可无提示读回；不同二进制/路径读
  同一条目可能触发钥匙串授权交互（macOS 访问控制按创建者记录）——daemon
  与 CLI 用同一解释器时不受影响；这个行为是 OS 决定的，不冒充可控。
- **`keychain_path`**：本版只支持默认 keychain——**范围裁决**（§2 复审更正：
  两轮探针对 SecItem 路由结论相反），不做语义不可依赖的假参数。
- **极端输入（复审记录在案）**：`set()` 收到含未配对代理的 str 会抛裸
  `UnicodeEncodeError`（未包一层）；account 含 NUL 会被 CFString 的 C 字符串
  语义截断（可能写到截短后的域）。无越界、无泄密；加固另开工单。
- **CI**：macOS runner 钥匙串状态以当次 run 为准；锁定时按 §3.4 skip。
- **锁文件/签名**：无新依赖，`uv lock` 不动。

## 7. 高危判定

凭据属敏感面但不碰授权判定/租约/并发写/事件日志/计费——F12 的授权语义
（能力声明只用于路由，红线 9）未变。按 CONTRIBUTING §7 不构成强制独立复审项，
但 PR 会显式请求第二读者关注（凭据面值得多一双眼睛）。

## 8. 实测回填（2026-11-14，macOS arm64 本机；数字经 2026-11-15 复审更正）

- `tests/test_credentials.py` 定向 **43 passed / 3 skipped**（skip 全部是非
  darwin 平台的 stub 断言，skip 原因写在消息里）；**净增 8 个测试函数**
  （diff 含 11 处 `+def`，其中 3 条为既有测试原位改写）；既有 keychain stub
  测试按 §3.4 平台条件化，其余（env/脱敏/事件库/CLI）原样全绿。
- 全量 `uv run --frozen pytest -o addopts="" -q -rs`：初版作者跑出 3203P/11S
  测于另一棵树（PR body 曾误绑 638f4e8）；**复审在 ed9568c 独立复跑 =
  3204 passed / 11 skipped（591.94s）exit0**。账面算术 3198 − 3（darwin 转
  skip）+ 净增 8 = 3203，与实测差 1，且基线计数在知识库里本就有 3198/3199
  两种记法——差 1 不强行归因，以复审实测为准。
- 负控三条实跑红还原（复审独立重放同样三条变异全红）：NC1（`-25300` 吞成
  `Secret("")`）→ `absent_is_none` 红；NC2（拆 duplicate→update）→
  `overwrite_uses_update` 红；NC3（`get()` 静默回退 `TIANSHU_API_KEY`）→
  防回退三连红。
- 门禁：`uv lock --check` exit0；`ruff check .` 全过——首轮 E501 是注释超长，
  且曾被「`ruff | tail` 管道吞退出码」的链式写法掩盖（仓库教训「错误必须显式
  传到会话退出码」的现身说法），已改为直接跑、真 0。
- 冒烟后复核：生产命名空间 `tianshu-agent` 清理归零，无测试残留。

## 9. 复审轮（2026-11-15，独立第二读者 + 修复）

独立第二读者（不读作者结论，只按 §4 判据 + 自设计探针 + 变异牙齿复审）在
ed9568c 复审本 PR：总结论 **PARTIAL、无 P0**——§4 十条判据全 PASS；三条负控
（NC1 吞 -25300 / NC2 拆 update / NC3 静默回退 env）独立重放全红；探针凭据
清理归零；kCFStringEncodingUTF8=0x08000100 实测正确。发现 2 P1 / 2 P2 / 5 P3，
当轮全部修复：

- **P1-1 三个测试直删生产凭据槽 `tianshu-agent/openai/llm`**（实测演示：植入
  假用户凭据 → 跑单测 → 凭据被无声清空；跑一次测试套件即销毁用户真实 key）。
  修复：autouse 夹具把本模块全部 keychain 测试隔离到 `tianshu-agent-selftest`
  service（补齐 §3.4 当初承诺的 service 级隔离——初版实现只做了 account 级
  唯一，属实现偏离 SPEC）+ 生产槽只读守卫（进入/退出双读比对；牙齿变异验证：
  种哨兵 → 直删生产槽 → 守卫红 → 还原；守卫消息只报「有值/空」不泄载荷）。
- **P1-2 「kSecUseKeychain 被忽略、文件路由已死」独立复现失败**（两臂对照
  探针：挂该键 SecItemAdd rc=0 且条目落进文件 keychain、login 0 命中；对照臂
  落 login）。修复：「只支持默认 keychain」改为**范围裁决**表述
  （`keychain_path` 仍 fail-closed，API 行为不变）；§2 增复审更正块。
- **P2×2**：PRD F12 行补落地状态（本 PR 合入后 macOS 已实现）；PR body 验证
  绑定曾误指 638f4e8（非本分支祖先）——改绑实际被审 SHA。
- **P3**：§8 数字账目更正；`default_store()` 错误文案与模块/类 docstring 过时
  叙事更新（「v1 只有 env」「macOS runner 没有 login keychain」均已被现实
  推翻）；locked 测试补 `kSecUseAuthenticationUIFail` 绑定常量断言；
  NUL / 未配对代理边角记入 §6。

修复后门禁（本机 macOS arm64）：`uv lock --check` exit0；`ruff check .` exit0；
全量 `uv run --frozen pytest -o addopts="" -q -rs` = **3204 passed / 11 skipped
（665.98s）** exit0；定向 `tests/test_credentials.py` = 43 passed / 3 skipped。
