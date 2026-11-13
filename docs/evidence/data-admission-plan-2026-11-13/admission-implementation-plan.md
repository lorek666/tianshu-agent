# 数据准入与迁移绑定 Implementation Plan

> **For agentic workers:** 实施获授权后，使用 `superpowers:executing-plans` 按下列任务逐项执行；作者与独立 verifier 分离。本文是待复审计划，不授权实现。步骤使用 checkbox 跟踪，当前均未执行。

**Goal:** 给实际 EventLog 文件增加兼容 v0 的只读准入前缀，并交付绑定完整一致点的标准根离线迁移；不实施 restore apply 或恢复隔离。

**Architecture:** `data_state` 只承载严格进度类型/解析，`data_admission` 在真实连接上执行只读准入，`data_digest` 计算完整逻辑/材料摘要；`migrate` 协调备份、独占锁、同一真实连接及两步事务。普通自定义 DB 保留，迁移只支持标准 ROOT/events.db，不新增 identity/generation 或 control/hold 表。

**Tech Stack:** Python `>=3.12,<3.13`、SQLite、纯标准库；测试沿用 pytest/ruff/uv，零第三方运行时依赖。

**Spec:** 权威输入为独立契约提交 `ccade5edbe47a66c0a48ad09e0667ee7393c148b:docs/data-admission-contract.md`；已直接读取Git对象并核对正文SHA256=`c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`，与保全副本 `/tmp/tianshu-g3-20261113/data-admission-contract-review-copy.md` 一致。独立设计review与契约独立commit已完成；**设计通过不等于实现验收**。规划原型仍绑定当时产品HEAD=`f509a53881c372d8d7f4b1e2718130edaff4ed06`，不把父会话当前正在同步的共享工作树当稳定输入。实施须等PR134收尾、最新稳定产品基线和父会话明确授权，并按基线差异更新受影响定位/验收。

## Global Constraints

- 全程只改批准范围；当前阶段仅写 `/tmp`。等待计划review收敛、最新稳定产品基线与父会话明确发放实施权。
- 普通产品 v0 不升级成 2；0→1→2 仍只是 migration_log 演示链，不代表全产品 schema 冻结。
- 事件、类型封闭集、记忆状态不变；不 UPDATE/DELETE 历史事件。
- `authority.db`、凭据、工作区不纳入本事件库准入；不新增数据库生命周期身份表。
- 不改 `loop.py`、host/exec/ipc/model/budget/net/recovery 模块或 CI。正常后台重开通过 EventLog 公共前缀自然覆盖。
- 维护锁只约束合作的当前程序；不承诺抵抗 raw SQLite、同 uid 篡改、路径替换竞争或旧客户端。
- 活源不用 immutable、不强制 checkpoint；保存的自包含 backup DB 可沿用已验证的 immutable reader。
- 保留四个 `migration-before-step-1/after-step-1/before-step-2/after-step-2` failpoint 及真实 CLI 硬退 77。

## Review Focus

- 完成迁移后的正常业务写入不能使日常 EventLog 再次被历史基线锁死；T1 正控测试覆盖。
- 缺少单行 event_versions 是既有 v0 映射，整个旁表缺失/坏形状不是“全体旧版”；T1 分别验证。
- 每个实际DB读取自身新format2的`<文件名>.migration.json`；仅标准events.db另读旧format1的migration.json，格式放错位置拒绝。旧格式1只有严格completed可供普通连接只读兼容，不能续做；T1双路径与历史夹具分别覆盖。
- actual v2普通连接至少有一份按格式/位置关联且严格合法completed；两关联文件全缺为migration_incomplete，不因机制对象合法而放行。data migrate还必须有合法format2 completed；legacy-only不获迁移权。
- T2首次绑定要求真实backup version等于live起始version且机制全快照相等；续做/原绑定重复要求真实backup version等于state.from_version，live差异只来自可达位置已提交步骤。
- 新空库在 SQLite 打开前排除非空孤立 WAL/journal、迁移进度与其它历史证据；T1 保留真正新空库正控。
- 当前源里多出的未引用材料、保存备份里多出的未声明材料也使完整一致点比较失败；T2 双向清单测试覆盖。
- SQLite 原生索引物理页不等于逻辑行；T2 包含 schema、全部持久索引表/FTS shadow 表及 integrity_check，不以原始 DB 文件哈希代替逻辑一致点。

## 1. 先冻结类型、API 与无循环依赖边界

### 1.1 模块职责与导入方向

```text
errors / stdlib
       ↑
data_state      event_schema（既有版本注册表；不初始化 store）
       ↑          ↑
data_admission ───┘           data_digest → stdlib / errors / data_state
       ↑
EventLog → maintenance         migrate → data_state / data_admission / data_digest
                                      → backup / maintenance / fsutil / crashpoint
                              data_cli → migrate（局部导入）/ 已有 backup API
```

`data_state` 不导入 migrate、backup、EventLog 或 store；`data_admission` 不导入 migrate/backup/EventLog/memory 或其他 store；`data_digest` 不导入它们，也不调用初始化器。`migrate` 可兼容 re-export 共享异常/状态类型和 read_state wrapper，不能另留一份宽松解析器。

已检查当前 backup 顶层没有 import events，**没有实证 backup→events 导入循环**。抽出 data_state 是为满足草稿 §4 的轻模块与共享严格解析边界，不是声称修复已复现循环。migrate 只拥有步骤、进度持久化、事务与报告，不再保有另一套宽松解析器。

### 1.2 `src/tianshu/data_state.py`（新建轻模块）

仅 stdlib + TianshuError。数据结构为 frozen dataclass，集合字段用 tuple；构造/解析拒绝不满足不变量的输入。

```python
DataFailure = Literal[
    'incompatible', 'migration_incomplete', 'progress_corrupt',
    'backup_mismatch', 'maintenance_busy', 'migration_failed',
]

class AdmissionRefused(TianshuError):
    code: DataFailure

class MigrationStateCorrupt(AdmissionRefused): ...  # code 固定 progress_corrupt

@dataclass(frozen=True)
class BaselineDigest:
    sqlite_algorithm: str      # 固定 tianshu-sqlite-logical-v1
    sqlite_sha256: str
    material_algorithm: str    # 固定 tianshu-material-inventory-v1
    material_sha256: str

@dataclass(frozen=True)
class BackupBinding:
    path: str                 # canonical backup 路径
    manifest_sha256: str       # 经验证的原始 backup.json 字节摘要

@dataclass(frozen=True)
class MigrationStateV2:
    db_path: str               # canonical 实际 main DB 路径
    from_version: int
    target_version: int
    status: Literal['in_progress', 'completed']
    applied_steps: tuple[int, ...]  # 从 from_version 开始的连续目标版本前缀
    current_step: int | None
    plan_sha256: str
    backup: BackupBinding
    baseline: BaselineDigest
    started_at_ms: int
    updated_at_ms: int

@dataclass(frozen=True)
class LegacyBackupV1:
    path: str
    format_version: int
    db_user_version: int
    event_count: int

@dataclass(frozen=True)
class LegacyStateV1:
    target_version: int
    status: Literal['in_progress', 'completed']
    applied_steps: tuple[int, ...]
    current_step: int | None
    backup: LegacyBackupV1
    started_at_ms: int
    updated_at_ms: int

MigrationState = MigrationStateV2 | LegacyStateV1
ProgressPosition = Literal['idle', 'before_step', 'after_step', 'completed']

# 只解析明确的两个版本；JSON加载另用重复key拒绝的object_pairs_hook。
def parse_state(raw: object) -> MigrationState: ...
def read_state_file(path: Path) -> MigrationState | None: ...
def associated_state_paths(db_path: Path) -> tuple[Path, ...]: ...
def encode_state(state: MigrationStateV2) -> bytes: ...
def classify_progress(state: MigrationStateV2, actual_version: int) -> ProgressPosition: ...
```

新写只用 format=`tianshu-migration`、format_version=2。JSON 重复 key、未知/缺失字段、未知格式版本、数字 bool/字符串/负值、坏 hex、非 canonical 的新绑定路径、重复/乱序/越界/有洞步骤一律报错；不调用 int()/str() 修输入，不丢弃坏字段。`applied_steps` 不包含 from_version 之前的旧步骤；实际机制审计另行核对其全量历史步骤。允许解析旧格式1不代表允许其续做：只有严格 target=2/status=completed/applied_steps=(1,2)/current=None、时间/旧backup四字段类型合法的旧记录，才交给普通准入在标准DB上核对历史兼容。旧in_progress继续拒绝，不改写/升级旧进度、不新造绑定。

`associated_state_paths`针对实际canonical DB返回自身`<db.name>.migration.json`，此位置只接受format2；只有canonical `ROOT/events.db`额外关联旧`ROOT/migration.json`，此旧位置只接受format1。format1放新sidecar或format2放旧位置一律progress_corrupt，不自动搬移/转换；先按位置核验再谈双文件共同事实。两者都存在就都严格读取和验证，不能凭优先级忽略坏记录。双completed比较的是共同的“已完成到v2”事实，不要求逐字节相等：每份各自严格合法且只关联实际canonical ROOT/events.db；legacy1必须target=2/status=completed/applied=(1,2)/current=None；format2必须db_path匹配、target=2/status=completed/current=None，applied等于from_version→2的完整序列（from0=(1,2)、from1=(2)、from2=()）；实际DB必须v2且完整两步机制schema/审计合法。时间、旧backup路径等不要求相同，也不能用legacy补造format2缺失绑定。任一坏记录、任何in_progress或实际目标/版本矛盾均拒绝，不删除、修改或忽略另一份。custom DB不读旧的无身份migration.json，但必须读自身的新路径。此处只解决记录共同事实，不把legacy1单独的普通连接兼容扩成新续做权。

`classify_progress` 只接受 §2 的 format2 可达组合，不把任意 state 数字向 DB 版本“对齐”。状态创建与更新都通过同一验证器；migrate 保留原公开 read_state(root) 名称时，转调上述关联文件解析/冲突检查，不保留旧coerce parser，所有新落盘目标固定为 `root/events.db.migration.json`。

### 1.3 `src/tianshu/data_admission.py`（新建）

```python
@dataclass(frozen=True)
class AdmissionSnapshot:
    db_path: str
    mechanism_version: int
    progress: MigrationState | None
    is_new: bool

# 无写入、无 store 构造；EventLog在打开文件前调用，排除历史旁证。
def check_new_file_evidence(db_path: Path) -> None: ...
# 供正常准入和独占迁移共享底层观察。
def inspect_connection(conn: sqlite3.Connection, db_path: Path) -> AdmissionSnapshot: ...
def check_connection(conn: sqlite3.Connection, db_path: Path) -> AdmissionSnapshot: ...
```

- 以 `PRAGMA database_list` 核对 conn 的 main 实际文件与 canonical db_path 相同，再读 schema/登记版本；不能只检查传入字符串指向的另一个文件。
- `:memory:` 走明确分支，不读 cwd 的 migration.json，不落维护状态。
- 已存在非空库需 events/event_versions 的可读列形状；已登记 payload 版本按真实 event_schema.known_versions(type) 校验，并检查 SQLite 存储类型；记忆已登记版本按冻结支持值校验，不能 import memory 触发 EventLog 反向依赖。测试把允许值与真实 memory._SCHEMA_VERSION 绑定以防漂移。
- 所有实际文件均读取自身`<数据库文件名>.migration.json`且仅接受format2，实际路径不符即拒绝。仅标准canonical events.db同时关联旧migration.json且仅接受format1，严格解析两份、核对冲突；自定义文件不借该旧无身份记录作证。合法JSON放错格式位置仍是progress_corrupt，不忽略它。
- actual v2至少有一份按格式/位置关联、严格合法的completed记录：format2，或标准DB旧位置的有限legacy1。两关联文件全缺即migration_incomplete拒绝，即使机制schema/audit完整合法；不能当新空、补造进度或静默放行。坏JSON为progress_corrupt，任何in_progress仍拒绝。
- 旧格式1 completed兼容仅限标准events.db和自身旧文件，核对实际DB=v2、两步audit/table/column/index与严格状态；只读放行，不执行旧续做。旧in_progress即使实际v2仍拒绝。
- `check_new_file_evidence` 在打开SQLite前检查：缺失/零字节主库若有非空WAL/journal、关联进度或其它已有历史证据则拒绝初始化；真实新空库允许。文件stat/read错误不得当作新空；已有非空库缺events仍保留EventLogCorrupt路径。
- `inspect_connection` 严格读取但不应用“普通连接禁止 in_progress/v1”的策略，供迁移协调器合法检查中间态；`check_connection` 加正常策略：新空/v0兼容、v1拒绝、v2检查迁移对象与严格进度、未来/坏登记版本拒绝、任何 in_progress 拒绝。
- completed 普通打开只核对文件/目标/完整步骤/实际版本/机制对象与记录合法性，**不将当前业务摘要与历史升级前基线相等作为日常打开条件**。

`EventLog._open_database` 取得现有共享锁后，先做只读check_new_file_evidence并保留原先是否非空的证据，再打开真实连接调用check_connection；通过才mkdir payload目录、设置WAL/FULL、执行原DDL。保留已有损坏events的fail-closed行为与所有异常关闭/锁释放路径。`:memory:`两个检查均走明确分支，不检查cwd或生成进度。

### 1.4 `src/tianshu/data_digest.py`（新建）与 migrate 协调 API

```python
@dataclass(frozen=True, order=True)
class MaterialRecord:
    path: str                 # root 相对 POSIX 路径
    size: int
    sha256: str

def sqlite_digest(conn: sqlite3.Connection) -> str: ...
# 独立于排除机制对象的business摘要；首次一致点全量比较与v2新备份no-op使用。
def mechanism_digest(conn: sqlite3.Connection) -> str: ...
def material_inventory(root: Path) -> tuple[MaterialRecord, ...]: ...
def material_digest(records: tuple[MaterialRecord, ...]) -> str: ...

# migrate 保留公开入口；内部一步改用同一真实连接，不再按路径重开。
def migrate(data_root: Path, *, backup: Path) -> MigrationReport: ...
def resume_migration(data_root: Path) -> MigrationReport: ...
def _apply_step(conn: sqlite3.Connection, step: MigrationStep) -> None: ...
```

算法 v1 的确切摘要输入：

1. 同一只读事务中读取 sqlite_schema 的 type/name/tbl_name/SQL、各持久表 column 元信息和类型化行；表名/列名正确 quote。包含全部普通表、sqlite_sequence、FTS 虚表及 shadow 表、向量表、未知业务扩展表；不使用手写业务表 allowlist。schema 中保留所有业务 index/trigger/view 定义。
2. 行编码带 NULL/TEXT/INTEGER/FLOAT64/BLOB 标签；浮点用明确 IEEE754 编码；行编码排序并保留重复行，不能依赖 SELECT 偶然顺序。可用的 rowid 也纳入；WITHOUT ROWID、generated/hidden 列需明确编码规则，不可稳定读取的形状拒绝迁移而非漏掉。
3. 唯一 DB 摘要排除项为经过形状核验的 migration_log 与本链明确拥有的 idx_migration_log_applied；user_version 单独验证。未知附着对象不随表名一起宽泛排除。原生 B-tree 不取物理页哈希：其定义与所属表行纳入，integrity_check 必须 ok；FTS/vector 的持久表行也纳入。全表摘要只证明相等，不证明业务 schema 已冻结。
4. 材料完整枚举 artifacts/**、blobs/**、memory_blobs/** 的普通文件，生成 path/size/SHA256 清单；不读根外、不跟随 symlink/junction，不只遍历“当前引用到的文件”。目录中出现被旧备份省略的隐藏/.tmp 材料也不能静默略过：保存清单没有对应项即拒绝迁移，既不扩大备份格式也不自动删文件。
5. 活根清单、保存备份实际清单、manifest 除 events.db 后的声明清单必须**集合双向相等**；缺项、多项、重复路径或内容/大小不同均拒绝。DB 文件本身由 verify_backup 核验保存材料，活源用逻辑摘要对比，不能拿主库 raw hash 忽略 WAL。
6. plan SHA256包含算法版本、精确排除对象、按序的from/to/description/SQL；当前仅允许这两项确定性SQL步骤。新增apply callback如没有可冻结的语义描述必须拒绝，不用函数repr/地址当计划摘要。
7. mechanism_digest覆盖实际user_version、migration_log完整table/column/index定义和全部类型化审计行，含step/applied_at/note等真实值；v0无机制对象也是明确快照。首次绑定与新备份no-op时live和保存库必须全量相等，只比step编号不够。续做不能简单要求机制digest相等，而须按§2验证从保存起点到live可达位置仅存在计划允许的机制变化。此快照不加入business baseline、不扩大正常EventLog日常基线核对。

migrate 在持独占准入期间打开**一条**真实 SQLite 连接；inspect/digest/BEGIN IMMEDIATE/每步DDL/版本与审计/完成前复查均用它。备份 snapshot 另开只读连接是比较的另一对象，不替实际迁移连接作证。

## 2. 简化绑定在两步链中是否足够

**在本片合作式锁、无业务写者、两步仅改变机制对象的范围内足够。** 临时规划原型已验证：真实业务库 34 张持久表（包含 budget、游标、summary、memory_fts 全部 shadow 表、sqlite_sequence、未知扩展表）在真实 `_apply_step` 的 v0/v1/v2 三个观察点逻辑摘要相同，保存快照也相同；整数1→文本'1'、新增业务表、额外 memory 材料均改变摘要。证据：`/tmp/tianshu-g3-20261113/admission-plan-probe.py` 与 `admission-plan-probe-results.json`。这是原型实验，不是新准入/进度/CLI 已实现。

首次新绑定只允许actual live v0/v1，且没有任何已有迁移进度。verify完整备份、source/别名匹配后，读取保存库真实PRAGMA而不是只信manifest：**backup.real_user_version == live.initial_user_version == state.from_version**，此时尚无允许的DDL差异，**mechanism_snapshot(backup) == mechanism_snapshot(live)**，完整定义和typed audit行均须相等；另比完整business逻辑摘要和双向材料。全部匹配才持久化绑定实际路径、起止版本、plan/manifest/baseline的format2进度，落盘失败不执行DDL。actual v2没有合法format2 completed（无记录或仅legacy1 completed）时，data migrate一律migration_incomplete拒绝且不创建/改写progress或DB；普通连接对有限legacy1的兼容不受影响。

续做/原绑定完成重复：重新验证同一backup路径、原manifest字节摘要/保存内容、同一实际DB路径、同一计划/算法、business与材料基线，并明确读取**backup.real_user_version == state.from_version**。live.actual_user_version只能是严格classify_progress允许的from/current/to位置；保存库到live的机制差异仅限在该位置已经提交的注册步骤，不能用相等business hash掩盖跨机制保存点。保存库完整起始schema/audit合法；此前已有audit行/值须保留，每个允许新增列/索引/审计行须对应计划已提交步骤，未来步骤对象、额外/改写旧audit或未登记DDL都拒绝。新步骤的时间值按真实持久审计校验，不能借“时间不同”放过继承行变化。只容忍这组可达状态（from=0时示例）：

- 初始：done=()，current=None，DB=0。
- step1 前：done=()，current=1，DB=0；step1 提交后但进度未收尾：同一 state，DB=1。
- step1 进度落盘：done=(1,)，current=None，DB=1。
- step2 前：done=(1,)，current=2，DB=1；step2 提交后但进度未收尾：同一 state，DB=2。
- 待完成标记：done=(1,2)，current=None，DB=2；随后 completed。

实际DB只能比**已登记的current_step**领先一步；从1开始时done只记录(2)。已有合法format2的from2完整序列为空，这只说明解析/验收规则，不授权v2 no-op新建进度。不使用 sorted(set(...)) 合并“声称已做”的数字，也不从 DB 版本无条件补齐 applied_steps。v2 的机制表/列/索引/审计行与每一步对应检查；from1 首次迁移保留并验证既有 step1 审计。

完成后的v2→v2是**只读no-op**：仍先检查标准布局、独占锁、同一真实连接、所有关联进度严格合法且相容、实际v2完整机制schema/审计合法。必须已有合法format2 completed；legacy1单独不获得新迁移/续做权。任何in_progress、坏格式、缺失/坏绑定或当前计划不符均拒绝。正常EventLog不重跑下面的迁移绑定。

- **A：本次backup的canonical路径等于原绑定路径。** 明确backup真实version==state.from_version，live为合法completed目标2且仅存在计划已提交步骤产生的机制变化；只能按原manifest字节摘要、plan/算法和business/material baseline核验同一次完成的重复；业务或材料已变时原备份拒绝。原manifest被替换、原备份丢失或任何绑定不符都拒绝；不得发现失败后转入“fresh no-op”绕过原绑定。
- **B：本次backup的canonical路径不同于原绑定路径。** 重新verify完整备份、source/别名匹配，备份真实user_version必须2；保存库的完整机制schema/审计与live当前点相符，使用mechanism_digest比较定义和完整typed audit行（包括applied_at/note），另比完整business摘要与双向材料inventory。此分支不要求当前business/material等于历史baseline，不要求旧backup仍存在；但format2原记录本身的结构、路径、plan和绑定字段仍须合法，legacy记录不能替它补字段。

A/B都不改绑、不写入/新建任何progress或DB行；不刷新updated_at、不重写审计。B报告from_version=2、to_version=2、applied_steps=[]、resumed=false、already_current=true、backup_path为本次新backup的canonical路径。A保持同一次完成幂等报告，且不能伪称再次应用步骤。

明确做不到的保证：

- 同 canonical path 且逻辑/材料完全相同的替换视作相同恢复点；没有 inode/UUID/generation 身份，不报告“检测到所有文件替换”。
- manifest 与内容同时被替换成**语义/字节完全相同**不能区分，也不必区分；任何实际绑定字节或逻辑差异必须拒绝。
- 同 uid raw writer 可绕过协作锁；不把重算摘要升级成对抗持续恶意写入的证明。
- 原型已证明同inode两个硬链接会得到不同canonical-path锁键。草稿§2.2已明确迁移DB须是根内无symlink普通文件、st_nlink=1；本片在创建进度或备份前拒绝不满足/不能确定的布局。验证canonical根与实际DB、查中间链接路径并在独占锁取得后复查，不能只resolve后漏掉原路径symlink。不修改普通custom DB行为、不声称已修所有旧backup/raw SQLite路径。
- 后续步骤若改业务表，当前“仅排除机制对象”的不变性不再足够，必须新增版本化转换证明/历史 fixture；不能沿用本片 hash 宣称任意迁移可续做。

## 3. 三个可独立验收的任务

任务在同一实施分支顺序落地；每项有自己的 RED/GREEN 与独立 review 边界，全部通过后才交付迁移入口。以下测试名/命令是**计划**，当前未写入仓库，未运行或声称绿色。

### Task 1：共享严格进度与实际连接准入

**Files**：新 data_state.py、data_admission.py；改 events.py 的打开前缀（现 `src/tianshu/events.py:295`、`:305`–`:368`）；新 tests/test_data_state.py、tests/test_data_admission.py；新固定tests/fixtures/data_admission/中的v0.sql及legacy-v2 SQL/状态JSON，内容取自绑定产品Git对象，不动态import新初始化器或新迁移器冒充历史。

**Interfaces**：产出 §1.2/§1.3；migrate 此时不复用旧 parser 给 admission 作证，Task2 再 re-export 共享类型。

- [ ] RED：先写状态语法/可达位置参数化测试，拒绝bool/字符串/负值/未知格式/重复key/重复与跳跃steps/矛盾completed。固定format1已完成旧库正控与坏类型/坏audit/旧in_progress负控，不用新迁移器动态造历史fixture。写真实EventLog/CLI测试 `test_future_actual_db_refused_before_ddl_and_blob_mkdir`、`test_v0_missing_version_row_allowed_but_missing_table_refused`、`test_missing_entire_event_versions_refused_after_close_reopen`（首次拒绝后关闭再重开也拒绝，不因新建空旁表洗白）、`test_progress_format_in_wrong_sidecar_refused`（format1新sidecar/format2旧位置）、`test_v2_without_progress_refuses_eventlog_and_cli_unchanged`（这里CLI指普通--db启动；机制/audit完整但两文件全缺，migration_incomplete且现场不变；data migrate对应负控在T2/T3）、`test_v2_valid_completed_opens_and_legacy_only_remains_ordinary_compatible`（合法completed正控，legacy-only普通兼容；migrate不授权负控留在T2，不在T1越界改协调器）、`test_bad_registered_memory_version_refused_before_alter`、`test_custom_db_ignores_unrelated_default_db_progress`、`test_custom_db_obeys_own_sidecar_progress`、`test_both_completed_records_share_fact_not_bytes`（from0/from1/from2分别正控，时间/旧backup路径可不同）、`test_both_progress_files_must_agree`（任一坏记录/in_progress/路径或版本矛盾拒绝，缺format2绑定不借legacy补造）、`test_zero_main_with_historical_evidence_refused_before_connect`、`test_memory_db_does_not_read_cwd_state`。
- [ ] 验证 RED：`uv run --frozen pytest -o addopts='' -q tests/test_data_state.py tests/test_data_admission.py`。预期新 parser/API 缺失或现有 EventLog 接受未来库/先建 blob 的断言失败，不能因 fixture 起不来/环境缺依赖算 RED。
- [ ] GREEN：实现只读 inspect/check 与严格 parser；EventLog 在真实连接检查后才 mkdir payload/WAL/DDL。保留旧事件缺单行版本映射；事件历史和正常 v0 的 user_version 不变；失败现场比较完整逻辑/schema+状态字节+材料，不能只比 event_count。
- [ ] GREEN 命令同上，再跑 `uv run --frozen pytest -o addopts='' -q tests/test_events.py tests/test_maintenance.py tests/test_memory_store.py`，预期目标全过，无新增 skip；加 `test_completed_state_does_not_freeze_later_business_writes` 正控。
- [ ] 独立验收：第二读者用 actual custom.sqlite=v999、无关 events.db=v2 真命令复核；真实后台重开通过 EventLog 的共同入口检查，不改 daemon/worker。测试用 AST/子进程 import 验证 admission 的禁止依赖，不能仅 grep 注释。

### Task 2：完整一致点绑定与严格续做库协议

**Files**：新data_digest.py；改migrate.py（解析委托/同连接步骤/绑定/续做）；新tests/test_data_binding.py；更新tests/test_migration.py的format2夹具与真实预期，旧测试按契约变更明确换成严格拒绝/只读兼容，不删减旧负例。nlink!=1、symlink/根外拒绝限migrate标准布局前置，不扩maintenance设计。

**Interfaces**：消费Task1的inspect_connection、严格state；产出§1.4，migrate重导出MigrationState联合类型、MigrationStateCorrupt及read_state(root)公共名；新状态构造显式MigrationStateV2，无第二份coerce parser。旧格式1无论是否completed均不签发新续做许可，migrate仅接受format2续做/完成重复，或无任何进度且actual v0/v1的新绑定；actual v2而无合法format2 completed（无记录或仅legacy）一律migration_incomplete拒绝且不写进度/DB，不能把普通连接兼容扩成隐式转换。

- [ ] RED：`test_binding_includes_every_persistent_table_and_schema_object`（全表枚举+真实stack，含FTS/向量/游标/summary/账本与未知表）、`test_fts_optimize_same_virtual_rows_changed_shadow_refuses_stale_backup`（真实多segment FTS后做optimize，先证虚拟查询结果相同且shadow持久行确已不同，再证拒绝旧保存点；不以直接篡改shadow替代）、`test_initial_binding_requires_real_version_and_full_mechanism_equality`（同root/business/material相同，只改backup机制version或typed audit即拒绝）、`test_resume_keeps_original_version_backup_as_valid_baseline`（合法原版本backup续做正控）、`test_resume_rejects_unplanned_inherited_audit_change`、`test_v2_without_format2_completed_refuses_without_creating_progress`（无记录/仅legacy分别负控，普通legacy兼容仍保留）、`test_typed_rows_distinguish_null_text_integer_float_blob`、`test_foreign_and_stale_backup_refused`、`test_projection_only_change_refused`、`test_live_extra_and_saved_undeclared_material_refused`、`test_manifest_or_plan_replacement_refuses_resume`、`test_resume_accepts_only_current_step_commit_window`、`test_completed_original_backup_path_never_falls_back_to_fresh_noop`、`test_completed_original_backup_rejects_later_business_or_material_change`、`test_completed_fresh_backup_noop_without_old_backup_or_rebinding`、`test_fresh_noop_compares_full_mechanism_audit_and_definitions`、`test_noop_refuses_in_progress_or_corrupt_state`。新备份正控在完成后真实业务写入，再在不同路径创建真实v2备份，并移走旧backup；报告和所有progress字节/DB逻辑前后均验证。负控分别只改保存库applied_at、note或机制定义，使step编号仍相同但必须拒绝。
- [ ] 验证 RED：`uv run --frozen pytest -o addopts='' -q tests/test_data_binding.py tests/test_migration.py`。预期新 digest/API 缺失，或现有迁移接受异根/过期/改投影/跳跃进度的负例失败；不能只用手写两张表替代真实业务基线。
- [ ] GREEN：独占锁持有期间同conn做检查/事务；严格标准根、nlink=1与无symlink布局，完整verify+双向材料+逻辑比较；需要执行升级步骤时才先写新events.db.migration.json的format2再DDL，每步机制/version/audit同事务；首次/续做明确执行§2真实版本等式和完整机制差异校验，actual v2无合法format2 completed拒绝；completed的A/B只读no-op遵守§2，不新建/重写进度或DB行。旧关联migration.json存在不能忽略或覆盖；不自动转换format1。保留四failpoint名称；失败不能写completed，关闭conn与释放锁可重复。
- [ ] GREEN 命令同上，再 `uv run --frozen pytest -o addopts='' -q tests/test_backup.py tests/test_backup_materials.py tests/test_data_state.py tests/test_data_admission.py`，预期全过。正控覆盖未 checkpoint WAL、所有机制观察点业务digest不变、字节相同snapshot替换可比较、from1起步、已完成立即幂等。
- [ ] 独立验收：第二读者移除“额外活材料检查”或只hash events 时相关测试必须红；移除manifest/plan绑定、真实version/机制起点等式、FTS shadow内容或恢复旧数字合并逻辑时对应测试必须红；把同原路径失败降级fresh no-op、只比audit.step或在no-op刷新状态时，对应负控/字节不变断言必须红。临时负控隔离保存并还原，不混入候选提交。

### Task 3：标准根 CLI、真实跨进程与四点故障验收

**Files**：改 data_cli.py parser/run/output，现 `src/tianshu/data_cli.py:24`、`:280`；新 tests/test_data_migration_cli.py；更新 tests/test_data_cli.py:405，将原“migrate不存在”改为“有入口且缺必需备份拒绝”，restore apply 仍不存在。实施交付时同步 docs/backup-restore.md、docs/KNOWLEDGE.md §2/§8；不更改冻结契约来适配实现。

**Interfaces**：只调 Task2 的 migrate(root, backup=...)；错误按 DataFailure 分类并经现有 CLI 脱敏，不复制版本/路径/hash算法。

- [ ] RED：`test_cli_requires_explicit_standard_root_and_backup`、`test_cli_reports_six_distinct_failure_codes`、`test_child_eventlog_blocks_migration`、`test_exclusive_migration_blocks_child_eventlog_before_payload_creation`、`test_cli_four_failpoints_resume_idempotently`、`test_progress_or_ddl_write_failure_never_reports_completed`。真子进程 stdin/pipe/Event 屏障；不以 sleep 或同进程两连接代替。
- [ ] 验证 RED：`uv run --frozen pytest -o addopts='' -q tests/test_data_migration_cli.py tests/test_data_cli.py`；预期 migrate 子命令/结构化失败报告不存在、旧迁移未互斥。每个负例先证明到达声称的路径。
- [ ] GREEN：只新增 `data migrate --data-root ROOT --backup BACKUP [--json]`；成功0、前置/失败2、failpoint77。报告 code 区分 incompatible/migration_incomplete/progress_corrupt/backup_mismatch/maintenance_busy/migration_failed；不停止服务、不启动daemon、不applyrestore。
- [ ] GREEN命令同上；四点测试逐点断言77、机制版本/新路径严格进度位置、普通EventLog中间态拒绝、重新CLI续做0、再调用0、事件顺序与append-only不变。添加真实os.link与symlink布局用例，断言创建进度前拒绝；平台不支持链接构造时明确原生skip原因，不模拟已在Windows实测。
- [ ] 最终本地门禁：`uv lock --check`、`uv run --frozen pytest`、`uv run --frozen ruff check .`，均需真实0。查 changed files 无 TODO占位/test.skip/.only/伪测试，绑定完整源码/测试SHA、复现命令与未决项，独立 verifier 重跑关键负控。
- [ ] 发布入口前复核 Windows/macOS/Linux 现有 required CI，当前计划不改 CI 或分支保护；本机绿不冒充原生 CI。Conventional Commit body 保留原因/验证/未决；契约 PR 先行、实现另 PR，不直推 main。

## 4. 有限改动面与评审前置

必需产品面仅三新模块 + events.py/migrate.py/data_cli.py；不集中迁移业务 store，不改权限/计费/模型/恢复对账实现，不引入control/hold、restore apply、容量或GC。测试和验收文档随本片更新。

输入副本已经明确：共享严格解析归data_state、自定义DB使用自身新进度、标准events.db同时识别旧进度、legacy1 completed有限只读兼容和维护nlink=1。计划据此修改，不再把这些写成尚未决定。

独立设计review给出的两个窄裁定已写入：双completed各自严格合法、只比较共同完成到v2的事实；completed只读no-op按原backup路径A和不同新路径B分流，A失败不得降级，B完整比较当前机制审计/业务/材料且不依赖旧备份。裁定来源为review_admission_contract会话授权的直接消息，尚待其核对本版验收映射；并未授权产品实现。

本版已绑定独立契约commit ccade5edbe47a66c0a48ad09e0667ee7393c148b及相同正文c62d…b8584；补齐真实version/机制快照等式、v2无进度拒绝与具名负控，不改已审契约。实施前仍须绑定父会话提供的最新稳定产品基线并明确授权；如基线改变定位/验收，先更新受影响范围，不把规划原型当批准或实现证据。

本计划的三个任务仍全部 pending。当前只交付规划原型/证据与计划，未修改仓库，等待独立计划review最终结论、最新稳定产品基线及父会话明确实施权。
