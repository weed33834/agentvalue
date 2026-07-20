"""
评估状态枚举常量（单一来源）
所有模块应引用此处的常量，避免字符串散落导致不一致。
"""


class EvaluationStatus:
    """评估审批状态"""

    AI_DRAFTED = "ai_drafted"
    MANAGER_REVIEW = "manager_review"
    HR_AUDIT = "hr_audit"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = frozenset({AI_DRAFTED, MANAGER_REVIEW, HR_AUDIT, APPROVED, REJECTED})

    @classmethod
    def values(cls) -> list[str]:
        """返回所有合法状态值（用于 DB CHECK 约束、Pydantic Literal 等）"""
        return [
            cls.AI_DRAFTED,
            cls.MANAGER_REVIEW,
            cls.HR_AUDIT,
            cls.APPROVED,
            cls.REJECTED,
        ]
