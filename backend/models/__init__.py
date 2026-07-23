"""
SQLAlchemy 数据模型
"""

from models.constants import EvaluationStatus
from models.custom_tool import CustomTool
from models.feature_flag import FeatureFlag
from models.models import (
    ApiKey,
    ApprovalAction,
    AuditLog,
    CompanyKB,
    DimensionScore,
    Evaluation,
    EvaluationPeriod,
    EvidenceRef,
    Feedback,
    Memory,
    Notification,
    RawInput,
    ScheduledTask,
    ScheduledTaskRun,
    SearchConfig,
    Tenant,
    User,
    WebhookEvent,
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
# 配额 / 预算 / 计费模型 (按租户配额管理 + 成本预算告警 + API 计费账单)
from models.quota_models import (
    BillingRecord,
    BudgetAlert,
    QuotaUsageLog,
    TenantQuota,
)
# Agent 版本管理 + 多渠道发布 (AgentVersion / AgentPublishTarget)
from models.agent_version import AgentPublishTarget, AgentVersion
# 敏感词字典管理 (SensitiveWord / SensitiveWordCategory)
from models.sensitive_word import SensitiveWord, SensitiveWordCategory
# 告警通知通道 (Alert)
from models.alert_model import Alert
# 模型 Fallback 策略 (对标阿里百炼 AI 网关秒级容灾)
from models.model_fallback import FallbackChain
# 会话分析看板 (对标 Langfuse Token 分析 / Dashboard)
from models.conversation_analytics import ConversationMetrics
# API 健康监控 (对标 Langfuse 延迟监控 / 告警系统)
from models.api_health import ApiHealthMetric, SloDefinition
# 数据集管理 (对标 Langfuse 数据集管理 + 阿里百炼训练集/评测集)
from models.dataset_models import DatasetItem, EvaluationDataset
# LLM-as-a-Judge 自动评测 (对标 Langfuse LLM-as-a-Judge + Dify 日志回放)
from models.evaluation_models import EvaluationResult, EvaluationTask
# RAG 质量评测 (对标 RagFlow 检索测试 + 压力测试)
from models.rag_eval_models import RagEvalResult, RagEvalTask
# 人工标注工具 (对标 Langfuse Human-in-the-loop)
from models.annotation_models import Annotation, AnnotationTask
# SSO 单点登录 (对标 Dify SSO / Bisheng SSO, OAuth2/SAML/LDAP)
from models.sso_models import SSOConfig, SSOSession
# Agent 模板市场 (对标 Coze 插件市场 / LobeChat 助手市场)
from models.agent_template_models import AgentTemplate, TemplateReview
# NL2SQL 自然语言转 SQL (对标 RagFlow NL2SQL)
from models.nl2sql_models import NL2SQLQuery, NL2SQLSchema
# 深度文档解析 (对标 RagFlow DeepDoc, 表格提取 + 版面分析)
from models.doc_parsing_models import DocParsingResult, DocParsingTask
# GraphRAG 知识图谱 (对标 RagFlow GraphRAG + RAPTOR, 实体关系抽取 + 图增强检索)
from models.knowledge_graph_models import (
    KnowledgeGraphEntity,
    KnowledgeGraphRelation,
    KnowledgeGraphTask,
)
# 灰度发布 / 蓝绿部署 (对标 Bisheng/Langfuse Canary 发布)
from models.gray_release_models import GrayRelease
# 多环境管理 (对标 Bisheng/Langfuse 环境隔离, dev/staging/prod 配置隔离)
from models.environment_models import Environment, EnvironmentDeployment
# 知识库自动同步 (对标 RagFlow 自动同步 / 阿里百炼数据源管理)
from models.kb_sync_models import KbDataSource, KbSyncLog
# Prompt 优化建议 (对标 Langfuse LLM Playground 交互测试)
from models.prompt_optimization_models import PromptOptimizationTask
# 模型负载均衡 (对标阿里百炼 AI 网关 GPU 感知负载均衡)
from models.model_load_balancer_models import LoadBalancerConfig, ModelInstance

__all__ = [
    "Tenant",
    "User",
    "ApiKey",
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
    # 定时任务调度
    "ScheduledTask",
    "ScheduledTaskRun",
    # 通知
    "Notification",
    # Webhook 事件
    "WebhookEvent",
    # 混合检索配置
    "SearchConfig",
    # 配额 / 预算 / 计费
    "TenantQuota",
    "QuotaUsageLog",
    "BudgetAlert",
    "BillingRecord",
    # Agent 版本管理 + 多渠道发布
    "AgentVersion",
    "AgentPublishTarget",
    # 敏感词字典管理
    "SensitiveWord",
    "SensitiveWordCategory",
    # 告警通知通道
    "Alert",
    # 模型 Fallback 策略
    "FallbackChain",
    # 会话分析看板
    "ConversationMetrics",
    # API 健康监控
    "ApiHealthMetric",
    "SloDefinition",
    # 数据集管理
    "EvaluationDataset",
    "DatasetItem",
    # LLM-as-a-Judge 自动评测
    "EvaluationTask",
    "EvaluationResult",
    # RAG 质量评测
    "RagEvalTask",
    "RagEvalResult",
    # 人工标注工具
    "AnnotationTask",
    "Annotation",
    # SSO 单点登录
    "SSOConfig",
    "SSOSession",
    # Agent 模板市场
    "AgentTemplate",
    "TemplateReview",
    # NL2SQL 自然语言转 SQL
    "NL2SQLQuery",
    "NL2SQLSchema",
    # 深度文档解析
    "DocParsingTask",
    "DocParsingResult",
    # GraphRAG 知识图谱
    "KnowledgeGraphEntity",
    "KnowledgeGraphRelation",
    "KnowledgeGraphTask",
    # 灰度发布 / 蓝绿部署
    "GrayRelease",
    # 多环境管理
    "Environment",
    "EnvironmentDeployment",
    # 知识库自动同步
    "KbDataSource",
    "KbSyncLog",
    # Prompt 优化建议
    "PromptOptimizationTask",
    # 模型负载均衡
    "ModelInstance",
    "LoadBalancerConfig",
]
