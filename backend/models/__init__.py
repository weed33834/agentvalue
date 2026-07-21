"""
SQLAlchemy 数据模型
"""

from models.constants import EvaluationStatus
from models.custom_tool import CustomTool
from models.feature_flag import FeatureFlag
from models.models import (
    ApprovalAction,
    AuditLog,
    CompanyKB,
    DimensionScore,
    Evaluation,
    EvaluationPeriod,
    EvidenceRef,
    Feedback,
    Memory,
    RawInput,
    Tenant,
    User,
)
# P4-2: 工作流可视化编排 (对标 Dify Workflow / Coze Bot 编排)
from models.workflow import Workflow, WorkflowRun
# Chat 会话模型 (移植 opencode Session/Message/Part 三层)
from models.chat_models import ChatMessage, ChatPart, ChatSession
# HR 评估增强: 360° 环评 + 校准会
from models.review_cycle import (
    REVIEW_STATUSES,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_SUBMITTED,
    REVIEWER_ROLES,
    REVIEWER_ROLE_EXTERNAL,
    REVIEWER_ROLE_MANAGER,
    REVIEWER_ROLE_PEER,
    REVIEWER_ROLE_SUBORDINATE,
    ReviewCycle,
)
from models.calibration import (
    CALIBRATION_STATUSES,
    CALIBRATION_STATUS_COMPLETED,
    CALIBRATION_STATUS_IN_PROGRESS,
    CALIBRATION_STATUS_SCHEDULED,
    CalibrationItem,
    CalibrationSession,
)
# 提示词模板库 + Agent预设 (对标 LobeChat/Open WebUI 模板 + ChatGPT GPTs)
# PromptTemplate 已合并到 models.models 中 (扩展了 category/content/variables 等列)
from models.prompt_template import AgentPreset
# Artifacts 可视化 (对标 Claude Artifacts / ChatGPT Canvas)
from models.artifact import Artifact
# Skills 系统 (对标 Claude Skills / Trae Skills)
from models.skill import Skill

__all__ = [
    "Tenant",
    "User",
    "RawInput",
    "Evaluation",
    "ApprovalAction",
    "AuditLog",
    "Feedback",
    "Memory",
    "CompanyKB",
    "EvaluationPeriod",
    "DimensionScore",
    "EvidenceRef",
    "EvaluationStatus",
    "CustomTool",
    "FeatureFlag",
    "Workflow",
    "WorkflowRun",
    "ChatSession",
    "ChatMessage",
    "ChatPart",
    # 360° 环评
    "ReviewCycle",
    "REVIEWER_ROLES",
    "REVIEWER_ROLE_PEER",
    "REVIEWER_ROLE_MANAGER",
    "REVIEWER_ROLE_SUBORDINATE",
    "REVIEWER_ROLE_EXTERNAL",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_PENDING",
    "REVIEW_STATUS_SUBMITTED",
    # 校准会
    "CalibrationSession",
    "CalibrationItem",
    "CALIBRATION_STATUSES",
    "CALIBRATION_STATUS_SCHEDULED",
    "CALIBRATION_STATUS_IN_PROGRESS",
    "CALIBRATION_STATUS_COMPLETED",
    # 提示词模板 + Agent预设
    "PromptTemplate",
    "AgentPreset",
    # Artifacts
    "Artifact",
    # Skills
    "Skill",
]
