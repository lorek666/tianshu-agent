"""C4 规则编号（事件 payload 里 `rule_id` 的封闭集合）。

`dispatch.requested` / `dispatch.rejected` / `policy.denied` 这三类事件的 payload
里都有一个 **用户可见** 的 `rule_id`：它回答"为什么被拒"，用户据此决定下一步。
在此之前它是一个隐式契约——25 种以上的字符串字面量散在 `dispatcher.py` 与
`permissions.py` 里，没有集合、没有校验、没有文档。这个模块把它变成和 C3
事件类型同级的封闭契约。

## 这些是**规则 id**，不是事件类型

`dispatch.lease_lost` 长得像事件类型，但它**只能**出现在 payload 的 `rule_id`
字段里。**不要**把它们登记进 `events.EVENT_TYPES`——那是事件类型的注册表，
混进去会让它替不存在的类型作证，正是 C3 要避免的那种撒谎。
`tests/test_rule_ids.py` 有一条断言钉死两个集合不相交。

## 三份清单

- `RULE_IDS`：**可以写出去的**全部编号（写入校验的唯一依据）。
- `DECLARED_UNUSED_RULE_IDS`：登记为合法、但**当前没有任何产生路径**的编号。
  保留它们是因为它们是**防御性兜底**（默认参数 / 最后的 else 分支）：真被触发时
  仍然要能记下一条拒绝，而不是让拒绝路径自己崩掉。这与 C3 的
  `DECLARED_UNEMITTED_EVENT_TYPES` 目的相同（不删除、显式标注），处理方式不同：
  事件类型写错永远是 bug，所以拒绝；规则 id 的兜底分支按设计就该能写，所以接受，
  但必须在文档里承认"没有已知的触发路径"。
- `DECISION_RULE_IDS`：不是拒绝，而是**判定结果**（自动放行 / 需审批）。它们同样
  会写进 `payload.rule_id`（`dispatch.requested` 的 `decision.rule_id`），所以也在
  `RULE_IDS` 里；区分开是为了让展示层能把"为什么被拒"和"为什么放行"分开讲。

## 机器可校验的副本

`schemas/c4/rules.json` 的 `rule_id.enum`；`tests/test_rule_ids.py` 把两边绑在一起，
改一处忘另一处会红。PRD §5.4.1 是给人看的那份（含"用户该怎么做"）。

## 改名

**不许改名**。历史事件日志里的 `rule_id` 是事后判断"用户当时为什么被拒"的唯一
线索，改名等于让新旧日志失去可比性。要新增就新增，并且同时改
`RULE_IDS` / `schemas/c4/rules.json` / PRD §5.4.1 三处。
"""

from __future__ import annotations

from .errors import TianshuError


class RuleIdError(TianshuError):
    """写出了一个未登记的规则编号。

    这是**代码 bug**，不是用户错误：用户在界面上看到的必须是
    "为什么被拒"，而不是"拒绝原因编号不存在"。所以宁可在这里炸掉，
    也不许把一个没登记过的字符串写进事件日志。
    """


#: 可以出现在 `payload.rule_id` 里的全部编号。
RULE_IDS: frozenset[str] = frozenset(
    {
        # ---- 身份失配：调用方声称的身份与库里的不一致 ----
        "dispatch.claim_mismatch",
        "dispatch.foreign_instance",
        "dispatch.task_mismatch",
        "dispatch.run_fenced",
        "dispatch.not_running",
        "dispatch.no_task",
        "dispatch.no_parent",
        "dispatch.stale_revision",
        # ---- 审批不可用：批过，但那张批准不能用在这个动作上 ----
        "dispatch.approval_revoked",
        "dispatch.approval_unusable",
        "c4.no_approval",
        "c4.approval_run_mismatch",
        "c4.approval_mismatch",
        "c4.approval_state",
        "c4.approval_expired",
        # ---- 人已经否决过这个动作：否决是终局，不再发起新审批（issue #40） ----
        "c4.approval_rejected",
        "c4.multi_resource",
        # ---- 租约与作业：身份没问题，是"现在不归你" ----
        "dispatch.claim_lost",
        "dispatch.lease_lost",
        "dispatch.guard_timeout",
        "dispatch.call_id_reused",
        # ---- 准入：工具本身不在白名单里 ----
        "dispatch.not_admitted",
        # ---- 前置条件：目标在被批准之后变了 ----
        "dispatch.stale_precondition",
        # ---- 目标不合法：覆盖/删除/新建作用在不该作用的路径上 ----
        "dispatch.target_is_directory",
        "dispatch.create_over_existing",
        "c4.create_over_existing",
        # ---- 工作区阻断：越出授权目录 / 只读绑定 / 无绑定 ----
        "dispatch.workspace_blocked",
        "c4.no_resource",
        "c4.readonly_binding",
        "c4.outside_workspace_unsupported",
        "c4.no_binding",
        # ---- 执行命令：最高危动作，必须先显式开启 exec 策略 ----
        "c4.exec_not_configured",
        "c4.exec_policy_denied",
        "c4.exec_unsupported",
        # ---- 记忆（F10）：作用域、种类与记忆目标 ----
        "c4.memory_scope",
        "c4.memory_scope_denied",
        "c4.memory_control_scope_binding",
        "c4.memory_kind_unsupported",
        "dispatch.memory_not_found",
        "dispatch.memory_stale_revision",
        "dispatch.memory_resource_mismatch",
        # ---- 判定结果（不是拒绝）：自动放行 / 需审批 ----
        "c4.auto",
        "c4.overwrite",
        "c4.delete",
        "c4.exec",
        "c4.outside_workspace",
        # ---- 兜底：正常情况下不该出现，真出现时必须能记下来 ----
        "dispatch.refused",
        "c4.unsupported",
        # ---- 保留未使用：登记为合法，但没有已知的产生路径 ----
        "dispatch.denied",
        "c4.unhandled",
    }
)

#: 登记为合法、但**当前没有产生路径**的编号。
#:
#: 两个都是防御性兜底，不是漏实现：
#:
#: - `dispatch.denied`：`DispatchDenied.__init__` 的 `rule_id` 默认值。现有 30 余处
#:   抛出点全部显式给了编号（`grep 'DispatchDenied('` 逐个确认），所以这个默认值
#:   一次都没被用到。保留它，是为了将来有人漏传时仍记下一条"未分类的拒绝"，
#:   而不是在拒绝路径里抛 `TypeError` 把原始错误盖掉。
#: - `c4.unhandled`：`PolicyEngine.classify()` 末尾的 else。当前
#:   `SUPPORTED_ACTIONS = {read, create, overwrite, delete}` 四个动作分别被
#:   AUTO / overwrite / delete 三个分支覆盖完，这一行**够不到**。保留它是为了
#:   将来往 `SUPPORTED_ACTIONS` 加动作时，忘了加分类分支会落到"未覆盖"而不是
#:   悄悄放行。
DECLARED_UNUSED_RULE_IDS: frozenset[str] = frozenset(
    {"dispatch.denied", "c4.unhandled"}
)

#: 判定结果（放行 / 需审批），不是拒绝。同样会写进 `payload.rule_id`。
DECISION_RULE_IDS: frozenset[str] = frozenset(
    {
        "c4.auto",
        "c4.overwrite",
        "c4.delete",
        "c4.exec",
        "c4.memory_scope",
        "c4.outside_workspace",
    }
)

#: 真正表示"被拒"的编号 = 全集 − 判定结果 − 保留未使用。
REJECTION_RULE_IDS: frozenset[str] = (
    RULE_IDS - DECISION_RULE_IDS - DECLARED_UNUSED_RULE_IDS
)

#: 语义分组，只用于文档与测试的自检（每个编号必须恰好属于一组）。
RULE_GROUPS: dict[str, frozenset[str]] = {
    "身份失配": frozenset(
        {
            "dispatch.claim_mismatch",
            "dispatch.foreign_instance",
            "dispatch.task_mismatch",
            "dispatch.run_fenced",
            "dispatch.not_running",
            "dispatch.no_task",
            "dispatch.no_parent",
            "dispatch.stale_revision",
        }
    ),
    "审批不可用": frozenset(
        {
            "dispatch.approval_revoked",
            "dispatch.approval_unusable",
            "c4.no_approval",
            "c4.approval_run_mismatch",
            "c4.approval_mismatch",
            "c4.approval_state",
            "c4.approval_expired",
            "c4.approval_rejected",
            "c4.multi_resource",
        }
    ),
    "租约与作业": frozenset(
        {
            "dispatch.claim_lost",
            "dispatch.lease_lost",
            "dispatch.guard_timeout",
            "dispatch.call_id_reused",
        }
    ),
    "准入": frozenset({"dispatch.not_admitted"}),
    "前置条件": frozenset({"dispatch.stale_precondition"}),
    "目标不合法": frozenset(
        {
            "dispatch.target_is_directory",
            "dispatch.create_over_existing",
            "c4.create_over_existing",
        }
    ),
    "工作区阻断": frozenset(
        {
            "dispatch.workspace_blocked",
            "c4.no_resource",
            "c4.readonly_binding",
            "c4.outside_workspace_unsupported",
            "c4.no_binding",
        }
    ),
    "执行（exec）": frozenset(
        {
            "c4.exec_not_configured",
            "c4.exec_policy_denied",
            "c4.exec_unsupported",
        }
    ),
    "记忆": frozenset(
        {
            "c4.memory_scope_denied",
            "c4.memory_control_scope_binding",
            "c4.memory_kind_unsupported",
            "dispatch.memory_not_found",
            "dispatch.memory_stale_revision",
            "dispatch.memory_resource_mismatch",
        }
    ),
    "判定结果（非拒绝）": DECISION_RULE_IDS,
    "兜底": frozenset({"dispatch.refused", "c4.unsupported"}),
    "保留未使用": DECLARED_UNUSED_RULE_IDS,
}


def check_rule_id(rule_id: str, *, where: str = "") -> str:
    """写入前的规则编号校验。

    必须在**产生任何副作用之前**调用：一个没登记的编号说明代码写错了，
    这时候最坏的选择是先把拒绝记进事件日志、再让排查的人去猜它是什么意思。
    """
    if rule_id in RULE_IDS:
        return rule_id
    site = f"（{where}）" if where else ""
    hint = ""
    if rule_id.startswith("dispatch.") or rule_id.startswith("c4."):
        hint = "：前缀正确但编号未登记"
    raise RuleIdError(
        f"未登记的 C4 规则编号：{rule_id!r}{site}。这是代码 bug，不是用户错误。"
        f"新增编号必须同时登记 src/tianshu/rules.py 的 RULE_IDS、"
        f"schemas/c4/rules.json 的 rule_id.enum 与 PRD §5.4.1{hint}"
    )
