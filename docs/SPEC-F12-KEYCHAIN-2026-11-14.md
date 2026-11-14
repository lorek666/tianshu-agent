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

**设计随之改定**：生产与测试都走**默认（login）keychain**，不做文件 keychain。

## 3. 设计

### 3.1 新模块 `keychain_macos.py`（ctypes 直调，非 darwin 不加载）

- 延迟加载：`credentials.py` 仅在 darwin 分支 `import keychain_macos`；
  模块顶层不 import ctypes 之外的平台库（零第三方运行时依赖不变，
  ctypes/util 是标准库）。
- 绑定面（全部实测过签名）：`SecKeychainOpen`、`SecItemAdd`、
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

「测试必须使用临时数据根」的纪律**在本工作流无法完全遵守**：探针证明这版
macOS 上不存在可用的文件 keychain 路由，隔离替身测不出真行为。处置：

- 测试条目用**每跑唯一的 service 名**（`tianshu-agent-selftest.<uuid4hex>`），
  与生产命名空间（`tianshu-agent`）和并行测试互不相撞；
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
- **`keychain_path`**：本版只支持默认 keychain；文件 keychain 探针证明已失效
  （§2），文档如实记，不做假参数。
- **CI**：macOS runner 钥匙串状态以当次 run 为准；锁定时按 §3.4 skip。
- **锁文件/签名**：无新依赖，`uv lock` 不动。

## 7. 高危判定

凭据属敏感面但不碰授权判定/租约/并发写/事件日志/计费——F12 的授权语义
（能力声明只用于路由，红线 9）未变。按 CONTRIBUTING §7 不构成强制独立复审项，
但 PR 会显式请求第二读者关注（凭据面值得多一双眼睛）。

## 8. 实测回填（2026-11-14，macOS arm64 本机）

- `tests/test_credentials.py` 定向 **43 passed / 3 skipped**（skip 全部是非
  darwin 平台的 stub 断言，skip 原因写在消息里）；新增 12 个测试函数、
  既有 4 条 keychain stub 测试按 §3.4 平台条件化，其余（env/脱敏/事件库/CLI）
  原样全绿。
- 全量 `uv run --frozen pytest -o addopts="" -q -rs`：**3203 passed / 11 skipped
  （580.88s）** exit0（基线 3198 + 本轮净增 8）。
- 负控三条实跑红还原：NC1（`-25300` 吞成 `Secret("")`）→ `absent_is_none` 红；
  NC2（拆 duplicate→update）→ `overwrite_uses_update` 红；
  NC3（`get()` 静默回退 `TIANSHU_API_KEY`）→ `macos_no_silent_env_fallback` 红。
- 门禁：`uv lock --check` exit0；`ruff check .` 全过——首轮 E501 是注释超长，
  且曾被「`ruff | tail` 管道吞退出码」的链式写法掩盖（仓库教训「错误必须显式
  传到会话退出码」的现身说法），已改为直接跑、真 0。
- 冒烟后复核：`security find-generic-password -s tianshu-agent` 命名空间
  清理归零，无测试残留。
