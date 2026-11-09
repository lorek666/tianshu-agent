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
