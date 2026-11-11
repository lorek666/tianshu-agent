"""错误类型。区分「模型想错了」与「执行侧跳过了校验」——后者是 bug，不是模型问题。"""


class TianshuError(Exception):
    """所有本库异常的基类。"""


class WorkspaceViolation(TianshuError):
    """路径越出 workspace。执行侧必须抛出，不允许静默降级。"""


class ToolNotAllowed(TianshuError):
    """工具不在白名单内。"""


class ToolArgumentError(TianshuError):
    """工具参数不满足 schema。"""


class ToolSchemaError(TianshuError):
    """工具注册表里的参数 schema 用了本方言不支持的写法。

    是**注册期**错误，不是运行期错误：一个写着 `oneOf` 的 schema，执行侧根本
    不会检查 `oneOf`，而模型和审批人会以为检查过。所以宁可 import 时就炸，
    也不许把"没实现的关键字"当注解静默忽略（PRD §5.2.3）。
    """


class OverwriteRefused(TianshuError):
    """目标文件已存在，拒绝覆盖（对应验收 A6）。"""


class EventLogCorrupt(TianshuError):
    """事件日志不可信：内容与记录对不上，或读不出结论。

    三种情形共用它：payload_hash 与实际内容不符（对应验收 A8）、外部 payload
    blob 缺失/不可读、以及阻断记录解析不出 workspace（无法判定哪些目录被阻断）。
    最后一种以前被静默跳过（复审 中-4），现在必须显式报出来。
    """


class SnapshotCorrupt(TianshuError):
    """写前证据（快照清单）读不出可信内容。

    和 `EventLogCorrupt` 是同一条道理，只是介质不同：事件日志是 SQLite，
    快照清单是 `artifacts/snapshots/<run>.json`。两者都是"这份材料不能作为
    结论来源"，所以都不能被静默当成空清单——那等于替一条读不懂的审计记录
    宣布"没写过"。调用方必须转成 unknown + 阻断，或者明确拒绝撤销。
    """


class WorkspaceBlocked(TianshuError):
    """workspace 上有未核实的副作用，禁止继续写入。"""


class BudgetExceeded(TianshuError):
    """轮数或 token 预算耗尽（对应验收 A5）。"""


class BudgetConfigError(TianshuError):
    """预算的**配置**进不了所请求的模式。

    目前只有一种来源：硬上限模式（`Limits.budget_bound = "upper"`）需要一个
    **可验证的输出上界**，而拿到上界的唯一途径是 adapter 的能力声明。拿不到
    就拒绝进入该模式——用默认的 256 token 输出预留冒充上界，等于把"估算"写成
    "保证"（PRD §5.6 第 3 条 / §5.7）。

    它与 `BudgetRefused` 的区别：那个是"这次请求被额度闸拒绝"，这个是"这一整套
    配置根本立不住"，所以在发出第一个请求之前就要失败。
    """


class BudgetRefused(TianshuError):
    """共享预算账本拒绝了这次预占（额度不足 / 已过 deadline / 上界被突破）。

    与 `BudgetExceeded` 的区别：后者是单 Run 的事后计数（轮数 / token 用完了），
    前者是**发送前**的原子预占失败。两个都不能被当成"暂时失败"重试——重试
    就是绕过闸门。
    """

    def __init__(
        self,
        message: str,
        *,
        account_id: str = "",
        dimension: str = "",
        limit: int | None = None,
        used: int | None = None,
        need: int | None = None,
    ) -> None:
        super().__init__(message)
        self.account_id = account_id
        self.dimension = dimension
        self.limit = limit
        self.used = used
        self.need = need


class BudgetCancelled(TianshuError):
    """预算账户已取消：拒绝一切新请求。

    已发出的请求保持原状（可能已计费），**不把取消当退款**（PRD §5.6 第 6 条）。
    """

    def __init__(self, message: str, *, account_id: str = "") -> None:
        super().__init__(message)
        self.account_id = account_id


class RunFenced(TianshuError):
    """Run 已被目标变更或租约变更 fence，不得继续写入。

    定义在 errors 而不是 tasks：执行侧（tools/dispatcher）也要抛这个语义，
    不能让执行层为了一个异常类型去 import 状态层。
    """


class CredentialUnsupported(TianshuError):
    """这个凭据后端/平台**做不到**这件事（例如 v1 的 OS Keychain）。

    与"没有配置凭据"**刻意不是一回事**：没配置是 `get() -> None` 的正常
    结果，可以靠设置环境变量解决；unsupported 是"这条路根本不存在"，
    再试也没用，只能换后端或等实现。两者合并会让用户去翻一个不存在的键，
    也会让"没配 key"变成崩溃。

    与 F2 的 `PolicyUnsupported` 同口径：做不到就报出来，**绝不降级**。
    """


class CredentialWriteRefused(TianshuError):
    """后端不支持写入（例如环境变量后端是进程环境的只读镜像）。

    单独一个类型而不是复用 `CredentialUnsupported`：能力存在、只是不支持
    写，和"整个后端不存在"是两件事，运维处置不同（前者去设环境变量，
    后者换后端）。
    """

