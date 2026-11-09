"""错误类型。区分「模型想错了」与「执行侧跳过了校验」——后者是 bug，不是模型问题。"""


class TianshuError(Exception):
    """所有本库异常的基类。"""


class WorkspaceViolation(TianshuError):
    """路径越出 workspace。执行侧必须抛出，不允许静默降级。"""


class ToolNotAllowed(TianshuError):
    """工具不在白名单内。"""


class ToolArgumentError(TianshuError):
    """工具参数不满足 schema。"""


class OverwriteRefused(TianshuError):
    """目标文件已存在，拒绝覆盖（对应验收 A6）。"""


class EventLogCorrupt(TianshuError):
    """事件日志的 payload_hash 与实际内容不符（对应验收 A8）。"""


class WorkspaceBlocked(TianshuError):
    """workspace 上有未核实的副作用，禁止继续写入。"""


class BudgetExceeded(TianshuError):
    """轮数或 token 预算耗尽（对应验收 A5）。"""


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

