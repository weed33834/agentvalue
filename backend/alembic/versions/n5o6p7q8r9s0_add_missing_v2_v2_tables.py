"""add missing v2 tables (补齐缺失的数据库表)

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
Create Date: 2026-07-29 00:00:00.000000

补齐所有 model 中定义但缺少 alembic 迁移的表（46 张）。
开发环境用 Base.metadata.create_all 自动建表所以没问题，但生产环境
alembic upgrade head 会缺这些表。本迁移一次性补齐。

覆盖的 model 文件与表:
- models/models.py: webhook_events, scheduled_tasks, scheduled_task_runs,
  notifications, api_keys, search_configs
- models/annotation_models.py: annotation_tasks, annotations
- models/sso_models.py: sso_configs, sso_sessions
- models/agent_template_models.py: agent_templates, agent_template_reviews
- models/dataset_models.py: evaluation_datasets, dataset_items
- models/evaluation_models.py: evaluation_tasks, evaluation_results
- models/quota_models.py: tenant_quotas, quota_usage_logs, budget_alerts,
  billing_records
- models/kb_sync_models.py: kb_data_sources, kb_sync_logs
- models/sensitive_word.py: sensitive_words, sensitive_word_categories
- models/doc_parsing_models.py: doc_parsing_tasks, doc_parsing_results
- models/environment_models.py: environments, environment_deployments
- models/alert_model.py: alerts
- models/agent_version.py: agent_versions, agent_publish_targets
- models/model_load_balancer_models.py: model_instances, load_balancer_configs
- models/gray_release_models.py: gray_releases
- models/conversation_analytics.py: conversation_metrics
- models/model_fallback.py: model_fallback_chains
- models/rag_eval_models.py: rag_eval_tasks, rag_eval_results
- models/api_health.py: api_health_metrics, slo_definitions
- models/prompt_optimization_models.py: prompt_optimization_tasks
- models/knowledge_graph_models.py: knowledge_graph_entities,
  knowledge_graph_relations, knowledge_graph_tasks
- models/nl2sql_models.py: nl2sql_queries, nl2sql_schemas

幂等: 用 inspector 检查表是否存在再 CREATE，兼容已通过 create_all 建表的环境。
表创建顺序按外键依赖排列（父表先于子表）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "n5o6p7q8r9s0"
down_revision: Union[str, Sequence[str], None] = "m4n5o6p7q8r9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 按外键依赖顺序排列（父表在前，子表在后），downgrade 用 reversed 顺序 drop
_NEW_TABLES = [
    # --- 独立表（无本迁移内部 FK 依赖）---
    "webhook_events",
    "scheduled_tasks",
    "scheduled_task_runs",
    "notifications",
    "api_keys",
    "search_configs",
    "annotation_tasks",
    "sso_configs",
    "sso_sessions",
    "agent_templates",
    "evaluation_datasets",
    "tenant_quotas",
    "quota_usage_logs",
    "budget_alerts",
    "billing_records",
    "kb_data_sources",
    "sensitive_words",
    "sensitive_word_categories",
    "doc_parsing_tasks",
    "environments",
    "alerts",
    "agent_versions",          # FK -> agent_presets (已有迁移 k2l3m4n5o6p7)
    "model_instances",
    "load_balancer_configs",
    "conversation_metrics",
    "model_fallback_chains",
    "rag_eval_tasks",
    "api_health_metrics",
    "slo_definitions",
    "prompt_optimization_tasks",
    "knowledge_graph_entities",
    "knowledge_graph_tasks",
    "nl2sql_queries",
    "nl2sql_schemas",
    # --- 依赖表（FK 指向本迁移创建的表）---
    "annotations",             # -> annotation_tasks
    "agent_template_reviews",  # -> agent_templates
    "dataset_items",           # -> evaluation_datasets
    "evaluation_tasks",        # -> evaluation_datasets
    "kb_sync_logs",            # -> kb_data_sources
    "doc_parsing_results",     # -> doc_parsing_tasks
    "agent_publish_targets",   # -> agent_presets, agent_versions
    "environment_deployments", # -> environments, agent_presets, agent_versions
    "gray_releases",           # -> agent_presets, agent_versions
    "knowledge_graph_relations",  # -> knowledge_graph_entities
    "rag_eval_results",        # -> rag_eval_tasks
    "evaluation_results",      # -> evaluation_tasks, dataset_items
]


def _has_table(inspector, name: str) -> bool:
    try:
        return name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    """Upgrade schema: 补齐所有缺失的表."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ==================================================================
    # 1. webhook_events (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "webhook_events"):
        op.create_table(
            "webhook_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_webhook_events_tenant_id", "webhook_events", ["tenant_id"])
        op.create_index("ix_webhook_event_source_status", "webhook_events", ["source", "status"])
        op.create_index("ix_webhook_event_tenant_received", "webhook_events", ["tenant_id", "received_at"])

    # ==================================================================
    # 2. scheduled_tasks (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "scheduled_tasks"):
        op.create_table(
            "scheduled_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("cron_expression", sa.String(128), nullable=False),
            sa.Column("task_type", sa.String(32), nullable=False),
            sa.Column("config", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_status", sa.String(16), nullable=True),
            sa.Column("last_run_error", sa.Text(), nullable=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_scheduled_tasks_task_id", "scheduled_tasks", ["task_id"])
        op.create_index("ix_scheduled_tasks_tenant_id", "scheduled_tasks", ["tenant_id"])
        op.create_index("ix_scheduled_task_tenant_type", "scheduled_tasks", ["tenant_id", "task_type"])

    # ==================================================================
    # 3. scheduled_task_runs (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "scheduled_task_runs"):
        op.create_table(
            "scheduled_task_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("result", sa.Text(), nullable=True),
            sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        )
        op.create_index("ix_scheduled_task_runs_task_id", "scheduled_task_runs", ["task_id"])
        op.create_index("ix_scheduled_task_runs_tenant_id", "scheduled_task_runs", ["tenant_id"])
        op.create_index("ix_task_run_task_started", "scheduled_task_runs", ["task_id", "started_at"])

    # ==================================================================
    # 4. notifications (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("notification_id", sa.String(64), nullable=False, unique=True),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("type", sa.String(32), nullable=False, server_default="system"),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("link", sa.String(512), nullable=True),
            sa.Column("category", sa.String(32), nullable=False, server_default="system"),
            sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_notifications_notification_id", "notifications", ["notification_id"])
        op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
        op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
        op.create_index("ix_notif_tenant_user_read", "notifications", ["tenant_id", "user_id", "is_read"])

    # ==================================================================
    # 5. api_keys (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "api_keys"):
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key_id", sa.String(64), nullable=False, unique=True),
            sa.Column("key_hash", sa.String(256), nullable=False),
            sa.Column("key_prefix", sa.String(16), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("scopes", sa.Text(), nullable=True),
            sa.Column("rate_limit", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_api_keys_key_id", "api_keys", ["key_id"])
        op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
        op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
        op.create_index("ix_apikey_tenant_active", "api_keys", ["tenant_id", "is_active"])

    # ==================================================================
    # 6. search_configs (models/models.py)
    # ==================================================================
    if not _has_table(inspector, "search_configs"):
        op.create_table(
            "search_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("config_key", sa.String(64), nullable=False),
            sa.Column("config_value", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "config_key", name="uix_tenant_search_config_key"),
        )
        op.create_index("ix_search_configs_config_key", "search_configs", ["config_key"])
        op.create_index("ix_search_configs_tenant_id", "search_configs", ["tenant_id"])

    # ==================================================================
    # 7. annotation_tasks (models/annotation_models.py)
    # ==================================================================
    if not _has_table(inspector, "annotation_tasks"):
        op.create_table(
            "annotation_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source_type", sa.String(32), nullable=False, server_default="agent_output"),
            sa.Column("source_id", sa.String(128), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("assigned_to", sa.String(64), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_annotation_tasks_tenant_id", "annotation_tasks", ["tenant_id"])
        op.create_index("ix_annotation_tasks_name", "annotation_tasks", ["name"])
        op.create_index("ix_annotation_tasks_status", "annotation_tasks", ["status"])
        op.create_index("ix_annotation_task_tenant_status", "annotation_tasks", ["tenant_id", "status"])
        op.create_index("ix_annotation_task_tenant_assignee", "annotation_tasks", ["tenant_id", "assigned_to"])

    # ==================================================================
    # 8. sso_configs (models/sso_models.py)
    # ==================================================================
    if not _has_table(inspector, "sso_configs"):
        op.create_table(
            "sso_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("provider_name", sa.String(128), nullable=False),
            sa.Column("provider_type", sa.String(16), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "provider_name", name="uix_tenant_sso_provider"),
        )
        op.create_index("ix_sso_configs_tenant_id", "sso_configs", ["tenant_id"])
        op.create_index("ix_sso_configs_provider_name", "sso_configs", ["provider_name"])
        op.create_index("ix_sso_config_tenant_type", "sso_configs", ["tenant_id", "provider_type"])

    # ==================================================================
    # 9. sso_sessions (models/sso_models.py)
    # ==================================================================
    if not _has_table(inspector, "sso_sessions"):
        op.create_table(
            "sso_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("provider_name", sa.String(128), nullable=False),
            sa.Column("external_user_id", sa.String(256), nullable=False),
            sa.Column("internal_user_id", sa.String(64), nullable=False),
            sa.Column("access_token", sa.Text(), nullable=True),
            sa.Column("refresh_token", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sso_sessions_tenant_id", "sso_sessions", ["tenant_id"])
        op.create_index("ix_sso_sessions_provider_name", "sso_sessions", ["provider_name"])
        op.create_index("ix_sso_sessions_external_user_id", "sso_sessions", ["external_user_id"])
        op.create_index("ix_sso_sessions_internal_user_id", "sso_sessions", ["internal_user_id"])
        op.create_index("ix_sso_session_tenant_external", "sso_sessions", ["tenant_id", "external_user_id"])
        op.create_index("ix_sso_session_internal", "sso_sessions", ["internal_user_id"])

    # ==================================================================
    # 10. agent_templates (models/agent_template_models.py)
    # ==================================================================
    if not _has_table(inspector, "agent_templates"):
        op.create_table(
            "agent_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("category", sa.String(32), nullable=False, server_default="general"),
            sa.Column("template_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("author", sa.String(128), nullable=True),
            sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rating", sa.Float(), nullable=False, server_default="0"),
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_official", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_agent_templates_tenant_id", "agent_templates", ["tenant_id"])
        op.create_index("ix_agent_templates_category", "agent_templates", ["category"])
        op.create_index("ix_agent_template_tenant_category", "agent_templates", ["tenant_id", "category"])
        op.create_index("ix_agent_template_public", "agent_templates", ["is_public"])
        op.create_index("ix_agent_template_official", "agent_templates", ["is_official"])

    # ==================================================================
    # 11. evaluation_datasets (models/dataset_models.py)
    # ==================================================================
    if not _has_table(inspector, "evaluation_datasets"):
        op.create_table(
            "evaluation_datasets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("dataset_type", sa.String(16), nullable=False, server_default="test"),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_evaluation_datasets_tenant_id", "evaluation_datasets", ["tenant_id"])
        op.create_index("ix_evaluation_datasets_name", "evaluation_datasets", ["name"])
        op.create_index("ix_evaluation_datasets_dataset_type", "evaluation_datasets", ["dataset_type"])
        op.create_index("ix_eval_dataset_tenant_type", "evaluation_datasets", ["tenant_id", "dataset_type"])

    # ==================================================================
    # 12. tenant_quotas (models/quota_models.py)
    # ==================================================================
    if not _has_table(inspector, "tenant_quotas"):
        op.create_table(
            "tenant_quotas",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, unique=True),
            sa.Column("max_requests_per_day", sa.Integer(), nullable=False, server_default="1000"),
            sa.Column("max_tokens_per_day", sa.Integer(), nullable=False, server_default="500000"),
            sa.Column("max_api_keys", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("current_requests_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("current_tokens_today", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quota_reset_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("max_requests_per_day >= 0", name="ck_quota_max_requests"),
            sa.CheckConstraint("max_tokens_per_day >= 0", name="ck_quota_max_tokens"),
            sa.CheckConstraint("max_api_keys >= 0", name="ck_quota_max_api_keys"),
            sa.CheckConstraint("current_requests_today >= 0", name="ck_quota_current_requests"),
            sa.CheckConstraint("current_tokens_today >= 0", name="ck_quota_current_tokens"),
        )
        op.create_index("ix_tenant_quotas_tenant_id", "tenant_quotas", ["tenant_id"])

    # ==================================================================
    # 13. quota_usage_logs (models/quota_models.py)
    # ==================================================================
    if not _has_table(inspector, "quota_usage_logs"):
        op.create_table(
            "quota_usage_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("usage_date", sa.String(10), nullable=False),
            sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_quota_usage_logs_tenant_id", "quota_usage_logs", ["tenant_id"])
        op.create_index("ix_quota_usage_logs_usage_date", "quota_usage_logs", ["usage_date"])
        op.create_index("uix_quota_usage_tenant_date", "quota_usage_logs", ["tenant_id", "usage_date"], unique=True)

    # ==================================================================
    # 14. budget_alerts (models/quota_models.py)
    # ==================================================================
    if not _has_table(inspector, "budget_alerts"):
        op.create_table(
            "budget_alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("budget_type", sa.String(16), nullable=False, server_default="monthly"),
            sa.Column("budget_limit", sa.Float(), nullable=False),
            sa.Column("current_usage", sa.Float(), nullable=False, server_default="0"),
            sa.Column("alert_threshold", sa.Float(), nullable=False, server_default="0.8"),
            sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("budget_limit >= 0", name="ck_budget_limit_positive"),
            sa.CheckConstraint("current_usage >= 0", name="ck_budget_usage_positive"),
            sa.CheckConstraint("alert_threshold > 0 AND alert_threshold <= 1", name="ck_budget_threshold_range"),
        )
        op.create_index("ix_budget_alerts_tenant_id", "budget_alerts", ["tenant_id"])
        op.create_index("ix_budget_tenant_type", "budget_alerts", ["tenant_id", "budget_type"])

    # ==================================================================
    # 15. billing_records (models/quota_models.py)
    # ==================================================================
    if not _has_table(inspector, "billing_records"):
        op.create_table(
            "billing_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("api_endpoint", sa.String(512), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column("billed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("invoice_period", sa.String(16), nullable=False),
        )
        op.create_index("ix_billing_records_tenant_id", "billing_records", ["tenant_id"])
        op.create_index("ix_billing_records_user_id", "billing_records", ["user_id"])
        op.create_index("ix_billing_records_invoice_period", "billing_records", ["invoice_period"])
        op.create_index("ix_billing_tenant_billed", "billing_records", ["tenant_id", "billed_at"])
        op.create_index("ix_billing_tenant_user", "billing_records", ["tenant_id", "user_id"])
        op.create_index("ix_billing_tenant_endpoint", "billing_records", ["tenant_id", "api_endpoint"])

    # ==================================================================
    # 16. kb_data_sources (models/kb_sync_models.py)
    # ==================================================================
    if not _has_table(inspector, "kb_data_sources"):
        op.create_table(
            "kb_data_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("collection_name", sa.String(256), nullable=False),
            sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_status", sa.String(16), nullable=False, server_default="never"),
            sa.Column("last_sync_stats", sa.JSON(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_kb_data_sources_tenant_id", "kb_data_sources", ["tenant_id"])
        op.create_index("ix_kb_ds_tenant_enabled", "kb_data_sources", ["tenant_id", "enabled"])

    # ==================================================================
    # 17. sensitive_words (models/sensitive_word.py)
    # ==================================================================
    if not _has_table(inspector, "sensitive_words"):
        op.create_table(
            "sensitive_words",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("word", sa.String(256), nullable=False),
            sa.Column("category", sa.String(32), nullable=False, server_default="custom"),
            sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
            sa.Column("action", sa.String(16), nullable=False, server_default="mask"),
            sa.Column("replacement", sa.String(256), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("category", "word", name="uix_sensitive_word_category_word"),
        )
        op.create_index("ix_sensitive_words_tenant_id", "sensitive_words", ["tenant_id"])
        op.create_index("ix_sensitive_words_word", "sensitive_words", ["word"])
        op.create_index("ix_sensitive_words_category", "sensitive_words", ["category"])
        op.create_index("ix_sensitive_word_active", "sensitive_words", ["is_active"])

    # ==================================================================
    # 18. sensitive_word_categories (models/sensitive_word.py)
    # ==================================================================
    if not _has_table(inspector, "sensitive_word_categories"):
        op.create_table(
            "sensitive_word_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(32), nullable=False, unique=True),
            sa.Column("description", sa.String(256), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        )
        op.create_index("ix_sensitive_word_categories_tenant_id", "sensitive_word_categories", ["tenant_id"])
        op.create_index("ix_sensitive_word_categories_name", "sensitive_word_categories", ["name"])
        op.create_index("ix_sensitive_category_active", "sensitive_word_categories", ["is_active"])

    # ==================================================================
    # 19. doc_parsing_tasks (models/doc_parsing_models.py)
    # ==================================================================
    if not _has_table(inspector, "doc_parsing_tasks"):
        op.create_table(
            "doc_parsing_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("file_path", sa.String(512), nullable=False),
            sa.Column("file_type", sa.String(16), nullable=False),
            sa.Column("parse_strategy", sa.String(16), nullable=False, server_default="auto"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("table_count", sa.Integer(), nullable=True),
            sa.Column("image_count", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_doc_parsing_tasks_tenant_id", "doc_parsing_tasks", ["tenant_id"])
        op.create_index("ix_doc_parsing_tasks_file_type", "doc_parsing_tasks", ["file_type"])
        op.create_index("ix_doc_parsing_tasks_status", "doc_parsing_tasks", ["status"])
        op.create_index("ix_doc_task_tenant_status", "doc_parsing_tasks", ["tenant_id", "status"])
        op.create_index("ix_doc_task_tenant_created", "doc_parsing_tasks", ["tenant_id", "created_at"])

    # ==================================================================
    # 20. environments (models/environment_models.py)
    # ==================================================================
    if not _has_table(inspector, "environments"):
        op.create_table(
            "environments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(32), nullable=False),
            sa.Column("display_name", sa.String(64), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("variables", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "name", name="uix_environment_tenant_name"),
        )
        op.create_index("ix_environments_tenant_id", "environments", ["tenant_id"])
        op.create_index("ix_environment_tenant_name", "environments", ["tenant_id", "name"])

    # ==================================================================
    # 21. alerts (models/alert_model.py)
    # ==================================================================
    if not _has_table(inspector, "alerts"):
        op.create_table(
            "alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("source", sa.String(64), nullable=False, server_default="system"),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(64), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(64), nullable=True),
        )
        op.create_index("ix_alerts_tenant_id", "alerts", ["tenant_id"])
        op.create_index("ix_alerts_severity", "alerts", ["severity"])
        op.create_index("ix_alerts_source", "alerts", ["source"])
        op.create_index("ix_alerts_status", "alerts", ["status"])
        op.create_index("ix_alert_status_severity", "alerts", ["status", "severity"])
        op.create_index("ix_alert_source_status", "alerts", ["source", "status"])
        op.create_index("ix_alert_created_at", "alerts", ["created_at"])

    # ==================================================================
    # 22. agent_versions (models/agent_version.py)
    #    FK -> agent_presets (已有迁移 k2l3m4n5o6p7 创建)
    # ==================================================================
    if not _has_table(inspector, "agent_versions"):
        op.create_table(
            "agent_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agent_presets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("system_prompt", sa.Text(), nullable=False),
            sa.Column("tools_config", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("model_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("temperature", sa.Integer(), nullable=False, server_default="70"),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("changelog", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("agent_id", "version_number", name="uix_agent_version_number"),
        )
        op.create_index("ix_agent_versions_tenant_id", "agent_versions", ["tenant_id"])
        op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])
        op.create_index("ix_agent_version_agent_status", "agent_versions", ["agent_id", "status"])

    # ==================================================================
    # 23. model_instances (models/model_load_balancer_models.py)
    # ==================================================================
    if not _has_table(inspector, "model_instances"):
        op.create_table(
            "model_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("base_url", sa.String(512), nullable=True),
            sa.Column("api_key_ref", sa.String(256), nullable=True),
            sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("current_load", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("health_status", sa.String(16), nullable=False, server_default="healthy"),
            sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
            sa.Column("avg_latency_ms", sa.Float(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_model_instances_tenant_id", "model_instances", ["tenant_id"])
        op.create_index("ix_model_instance_tenant_enabled", "model_instances", ["tenant_id", "enabled"])
        op.create_index("ix_model_instance_tenant_provider", "model_instances", ["tenant_id", "provider"])

    # ==================================================================
    # 24. load_balancer_configs (models/model_load_balancer_models.py)
    # ==================================================================
    if not _has_table(inspector, "load_balancer_configs"):
        op.create_table(
            "load_balancer_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("strategy", sa.String(32), nullable=False, server_default="round_robin"),
            sa.Column("instances", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_load_balancer_configs_tenant_id", "load_balancer_configs", ["tenant_id"])
        op.create_index("ix_lb_config_tenant_name", "load_balancer_configs", ["tenant_id", "name"])

    # ==================================================================
    # 25. conversation_metrics (models/conversation_analytics.py)
    # ==================================================================
    if not _has_table(inspector, "conversation_metrics"):
        op.create_table(
            "conversation_metrics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("conversation_id", sa.String(128), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=True),
            sa.Column("agent_id", sa.String(64), nullable=True),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("latency_ms", sa.Float(), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="success"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_conversation_metrics_tenant_id", "conversation_metrics", ["tenant_id"])
        op.create_index("ix_conversation_metrics_conversation_id", "conversation_metrics", ["conversation_id"])
        op.create_index("ix_conversation_metrics_user_id", "conversation_metrics", ["user_id"])
        op.create_index("ix_conversation_metrics_agent_id", "conversation_metrics", ["agent_id"])
        op.create_index("ix_conversation_metrics_model", "conversation_metrics", ["model"])
        op.create_index("ix_conv_metrics_tenant_time", "conversation_metrics", ["tenant_id", "timestamp"])
        op.create_index("ix_conv_metrics_tenant_user", "conversation_metrics", ["tenant_id", "user_id"])
        op.create_index("ix_conv_metrics_tenant_agent", "conversation_metrics", ["tenant_id", "agent_id"])
        op.create_index("ix_conv_metrics_tenant_model", "conversation_metrics", ["tenant_id", "model"])

    # ==================================================================
    # 26. model_fallback_chains (models/model_fallback.py)
    # ==================================================================
    if not _has_table(inspector, "model_fallback_chains"):
        op.create_table(
            "model_fallback_chains",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("chain_config", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_model_fallback_chains_tenant_id", "model_fallback_chains", ["tenant_id"])
        op.create_index("ix_fallback_tenant_priority", "model_fallback_chains", ["tenant_id", "priority"])

    # ==================================================================
    # 27. rag_eval_tasks (models/rag_eval_models.py)
    # ==================================================================
    if not _has_table(inspector, "rag_eval_tasks"):
        op.create_table(
            "rag_eval_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("collection_name", sa.String(128), nullable=False),
            sa.Column("test_queries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("total_queries", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_queries", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("results_summary", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_rag_eval_tasks_tenant_id", "rag_eval_tasks", ["tenant_id"])
        op.create_index("ix_rag_eval_tasks_name", "rag_eval_tasks", ["name"])
        op.create_index("ix_rag_eval_tasks_status", "rag_eval_tasks", ["status"])
        op.create_index("ix_rag_eval_task_tenant_status", "rag_eval_tasks", ["tenant_id", "status"])

    # ==================================================================
    # 28. api_health_metrics (models/api_health.py)
    # ==================================================================
    if not _has_table(inspector, "api_health_metrics"):
        op.create_table(
            "api_health_metrics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("endpoint", sa.String(512), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("response_time_ms", sa.Float(), nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_api_health_metrics_tenant_id", "api_health_metrics", ["tenant_id"])
        op.create_index("ix_api_health_metrics_endpoint", "api_health_metrics", ["endpoint"])
        op.create_index("ix_api_health_tenant_endpoint_time", "api_health_metrics", ["tenant_id", "endpoint", "timestamp"])

    # ==================================================================
    # 29. slo_definitions (models/api_health.py)
    # ==================================================================
    if not _has_table(inspector, "slo_definitions"):
        op.create_table(
            "slo_definitions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("endpoint", sa.String(512), nullable=False),
            sa.Column("target_latency_ms", sa.Float(), nullable=False),
            sa.Column("target_success_rate", sa.Float(), nullable=False, server_default="0.99"),
            sa.Column("window_minutes", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_slo_definitions_tenant_id", "slo_definitions", ["tenant_id"])
        op.create_index("ix_slo_definitions_endpoint", "slo_definitions", ["endpoint"])
        op.create_index("ix_slo_tenant_endpoint", "slo_definitions", ["tenant_id", "endpoint"])

    # ==================================================================
    # 30. prompt_optimization_tasks (models/prompt_optimization_models.py)
    # ==================================================================
    if not _has_table(inspector, "prompt_optimization_tasks"):
        op.create_table(
            "prompt_optimization_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("original_prompt", sa.Text(), nullable=False),
            sa.Column("optimized_prompt", sa.Text(), nullable=True),
            sa.Column("task_type", sa.String(32), nullable=False, server_default="improve"),
            sa.Column("model_used", sa.String(128), nullable=True),
            sa.Column("suggestions", sa.JSON(), nullable=True),
            sa.Column("quality_scores", sa.JSON(), nullable=True),
            sa.Column("overall_score", sa.Float(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_prompt_optimization_tasks_tenant_id", "prompt_optimization_tasks", ["tenant_id"])
        op.create_index("ix_prompt_optimization_tasks_status", "prompt_optimization_tasks", ["status"])
        op.create_index("ix_prompt_opt_tenant_status", "prompt_optimization_tasks", ["tenant_id", "status"])

    # ==================================================================
    # 31. knowledge_graph_entities (models/knowledge_graph_models.py)
    # ==================================================================
    if not _has_table(inspector, "knowledge_graph_entities"):
        op.create_table(
            "knowledge_graph_entities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("entity_type", sa.String(32), nullable=False, server_default="concept"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("properties", sa.JSON(), nullable=True),
            sa.Column("source_docs", sa.JSON(), nullable=True),
            sa.Column("embedding_id", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_graph_entities_tenant_id", "knowledge_graph_entities", ["tenant_id"])
        op.create_index("ix_kg_entity_tenant_name", "knowledge_graph_entities", ["tenant_id", "name"])
        op.create_index("ix_kg_entity_tenant_type", "knowledge_graph_entities", ["tenant_id", "entity_type"])

    # ==================================================================
    # 32. knowledge_graph_tasks (models/knowledge_graph_models.py)
    # ==================================================================
    if not _has_table(inspector, "knowledge_graph_tasks"):
        op.create_table(
            "knowledge_graph_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("collection_name", sa.String(128), nullable=False),
            sa.Column("document_ids", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("entity_count", sa.Integer(), nullable=True),
            sa.Column("relation_count", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_knowledge_graph_tasks_tenant_id", "knowledge_graph_tasks", ["tenant_id"])
        op.create_index("ix_knowledge_graph_tasks_status", "knowledge_graph_tasks", ["status"])
        op.create_index("ix_kg_task_tenant_status", "knowledge_graph_tasks", ["tenant_id", "status"])
        op.create_index("ix_kg_task_tenant_created", "knowledge_graph_tasks", ["tenant_id", "created_at"])

    # ==================================================================
    # 33. nl2sql_queries (models/nl2sql_models.py)
    # ==================================================================
    if not _has_table(inspector, "nl2sql_queries"):
        op.create_table(
            "nl2sql_queries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("natural_query", sa.Text(), nullable=False),
            sa.Column("generated_sql", sa.Text(), nullable=True),
            sa.Column("sql_explanation", sa.Text(), nullable=True),
            sa.Column("database_schema", sa.JSON(), nullable=True),
            sa.Column("table_name", sa.String(128), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("result_count", sa.Integer(), nullable=True),
            sa.Column("result_data", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_nl2sql_queries_tenant_id", "nl2sql_queries", ["tenant_id"])
        op.create_index("ix_nl2sql_queries_table_name", "nl2sql_queries", ["table_name"])
        op.create_index("ix_nl2sql_queries_status", "nl2sql_queries", ["status"])
        op.create_index("ix_nl2sql_query_tenant_created", "nl2sql_queries", ["tenant_id", "created_at"])
        op.create_index("ix_nl2sql_query_tenant_status", "nl2sql_queries", ["tenant_id", "status"])

    # ==================================================================
    # 34. nl2sql_schemas (models/nl2sql_models.py)
    # ==================================================================
    if not _has_table(inspector, "nl2sql_schemas"):
        op.create_table(
            "nl2sql_schemas",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("table_name", sa.String(128), nullable=False),
            sa.Column("schema_definition", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("sample_queries", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "table_name", name="uix_tenant_nl2sql_table"),
        )
        op.create_index("ix_nl2sql_schemas_tenant_id", "nl2sql_schemas", ["tenant_id"])
        op.create_index("ix_nl2sql_schemas_table_name", "nl2sql_schemas", ["table_name"])

    # ==================================================================
    # 35. annotations (models/annotation_models.py)  -> annotation_tasks
    # ==================================================================
    if not _has_table(inspector, "annotations"):
        op.create_table(
            "annotations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("annotation_tasks.id"), nullable=False),
            sa.Column("annotator_id", sa.String(64), nullable=False),
            sa.Column("label", sa.String(128), nullable=True),
            sa.Column("score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("feedback", sa.Text(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_annotations_tenant_id", "annotations", ["tenant_id"])
        op.create_index("ix_annotations_task_id", "annotations", ["task_id"])
        op.create_index("ix_annotations_annotator_id", "annotations", ["annotator_id"])
        op.create_index("ix_annotation_tenant_task", "annotations", ["tenant_id", "task_id"])
        op.create_index("ix_annotation_tenant_annotator", "annotations", ["tenant_id", "annotator_id"])

    # ==================================================================
    # 36. agent_template_reviews (models/agent_template_models.py)
    #     -> agent_templates
    # ==================================================================
    if not _has_table(inspector, "agent_template_reviews"):
        op.create_table(
            "agent_template_reviews",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("template_id", sa.Integer(), sa.ForeignKey("agent_templates.id", ondelete="CASCADE"), nullable=False),
            sa.Column("reviewer_id", sa.String(64), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "template_id", "reviewer_id", name="uix_tenant_template_reviewer"),
        )
        op.create_index("ix_agent_template_reviews_tenant_id", "agent_template_reviews", ["tenant_id"])
        op.create_index("ix_agent_template_reviews_template_id", "agent_template_reviews", ["template_id"])
        op.create_index("ix_template_review_template", "agent_template_reviews", ["template_id"])

    # ==================================================================
    # 37. dataset_items (models/dataset_models.py) -> evaluation_datasets
    # ==================================================================
    if not _has_table(inspector, "dataset_items"):
        op.create_table(
            "dataset_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("evaluation_datasets.id"), nullable=False),
            sa.Column("input", sa.JSON(), nullable=False),
            sa.Column("expected_output", sa.JSON(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("label", sa.String(128), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_dataset_items_tenant_id", "dataset_items", ["tenant_id"])
        op.create_index("ix_dataset_items_dataset_id", "dataset_items", ["dataset_id"])
        op.create_index("ix_dataset_items_status", "dataset_items", ["status"])
        op.create_index("ix_dataset_item_tenant_dataset", "dataset_items", ["tenant_id", "dataset_id"])
        op.create_index("ix_dataset_item_tenant_status", "dataset_items", ["tenant_id", "status"])

    # ==================================================================
    # 38. evaluation_tasks (models/evaluation_models.py)
    #     -> evaluation_datasets
    # ==================================================================
    if not _has_table(inspector, "evaluation_tasks"):
        op.create_table(
            "evaluation_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("evaluation_datasets.id"), nullable=False),
            sa.Column("judge_model", sa.String(32), nullable=False, server_default="L0"),
            sa.Column("judge_prompt_template", sa.Text(), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("results_summary", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_evaluation_tasks_tenant_id", "evaluation_tasks", ["tenant_id"])
        op.create_index("ix_evaluation_tasks_name", "evaluation_tasks", ["name"])
        op.create_index("ix_evaluation_tasks_dataset_id", "evaluation_tasks", ["dataset_id"])
        op.create_index("ix_evaluation_tasks_status", "evaluation_tasks", ["status"])
        op.create_index("ix_eval_task_tenant_dataset", "evaluation_tasks", ["tenant_id", "dataset_id"])
        op.create_index("ix_eval_task_tenant_status", "evaluation_tasks", ["tenant_id", "status"])

    # ==================================================================
    # 39. kb_sync_logs (models/kb_sync_models.py) -> kb_data_sources
    # ==================================================================
    if not _has_table(inspector, "kb_sync_logs"):
        op.create_table(
            "kb_sync_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("data_source_id", sa.Integer(), sa.ForeignKey("kb_data_sources.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sync_type", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stats", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
        )
        op.create_index("ix_kb_sync_logs_tenant_id", "kb_sync_logs", ["tenant_id"])
        op.create_index("ix_kb_sync_logs_data_source_id", "kb_sync_logs", ["data_source_id"])
        op.create_index("ix_kb_sync_log_tenant_ds", "kb_sync_logs", ["tenant_id", "data_source_id"])

    # ==================================================================
    # 40. doc_parsing_results (models/doc_parsing_models.py)
    #     -> doc_parsing_tasks
    # ==================================================================
    if not _has_table(inspector, "doc_parsing_results"):
        op.create_table(
            "doc_parsing_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("doc_parsing_tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("page_num", sa.Integer(), nullable=True),
            sa.Column("content_type", sa.String(16), nullable=False, server_default="text"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("bounding_box", sa.JSON(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_doc_parsing_results_tenant_id", "doc_parsing_results", ["tenant_id"])
        op.create_index("ix_doc_parsing_results_task_id", "doc_parsing_results", ["task_id"])
        op.create_index("ix_doc_parsing_results_page_num", "doc_parsing_results", ["page_num"])
        op.create_index("ix_doc_parsing_results_content_type", "doc_parsing_results", ["content_type"])
        op.create_index("ix_doc_result_task_page", "doc_parsing_results", ["task_id", "page_num"])
        op.create_index("ix_doc_result_task_type", "doc_parsing_results", ["task_id", "content_type"])

    # ==================================================================
    # 41. agent_publish_targets (models/agent_version.py)
    #     -> agent_presets, agent_versions
    # ==================================================================
    if not _has_table(inspector, "agent_publish_targets"):
        op.create_table(
            "agent_publish_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agent_presets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.UniqueConstraint("agent_id", "channel", name="uix_agent_publish_channel"),
        )
        op.create_index("ix_agent_publish_targets_tenant_id", "agent_publish_targets", ["tenant_id"])
        op.create_index("ix_agent_publish_targets_agent_id", "agent_publish_targets", ["agent_id"])
        op.create_index("ix_agent_publish_targets_version_id", "agent_publish_targets", ["version_id"])
        op.create_index("ix_agent_publish_agent_channel", "agent_publish_targets", ["agent_id", "channel"])

    # ==================================================================
    # 42. environment_deployments (models/environment_models.py)
    #     -> environments, agent_presets, agent_versions
    # ==================================================================
    if not _has_table(inspector, "environment_deployments"):
        op.create_table(
            "environment_deployments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("environment_id", sa.Integer(), sa.ForeignKey("environments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agent_presets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="deployed"),
            sa.Column("config_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("deployed_by", sa.String(64), nullable=True),
            sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("undeployed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_environment_deployments_tenant_id", "environment_deployments", ["tenant_id"])
        op.create_index("ix_environment_deployments_environment_id", "environment_deployments", ["environment_id"])
        op.create_index("ix_environment_deployments_agent_id", "environment_deployments", ["agent_id"])
        op.create_index("ix_environment_deployments_version_id", "environment_deployments", ["version_id"])
        op.create_index("ix_env_deploy_tenant_env", "environment_deployments", ["tenant_id", "environment_id"])
        op.create_index("ix_env_deploy_tenant_agent", "environment_deployments", ["tenant_id", "agent_id"])

    # ==================================================================
    # 43. gray_releases (models/gray_release_models.py)
    #     -> agent_presets, agent_versions
    # ==================================================================
    if not _has_table(inspector, "gray_releases"):
        op.create_table(
            "gray_releases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agent_presets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("release_type", sa.String(16), nullable=False, server_default="canary"),
            sa.Column("traffic_percentage", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("traffic_percentage >= 0 AND traffic_percentage <= 100", name="ck_gray_release_traffic_range"),
        )
        op.create_index("ix_gray_releases_tenant_id", "gray_releases", ["tenant_id"])
        op.create_index("ix_gray_releases_agent_id", "gray_releases", ["agent_id"])
        op.create_index("ix_gray_releases_version_id", "gray_releases", ["version_id"])
        op.create_index("ix_gray_release_tenant_status", "gray_releases", ["tenant_id", "status"])
        op.create_index("ix_gray_release_tenant_agent", "gray_releases", ["tenant_id", "agent_id"])

    # ==================================================================
    # 44. knowledge_graph_relations (models/knowledge_graph_models.py)
    #     -> knowledge_graph_entities (x2: source + target)
    # ==================================================================
    if not _has_table(inspector, "knowledge_graph_relations"):
        op.create_table(
            "knowledge_graph_relations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("source_entity_id", sa.Integer(), sa.ForeignKey("knowledge_graph_entities.id", ondelete="CASCADE"), nullable=False),
            sa.Column("target_entity_id", sa.Integer(), sa.ForeignKey("knowledge_graph_entities.id", ondelete="CASCADE"), nullable=False),
            sa.Column("relation_type", sa.String(32), nullable=False, server_default="related_to"),
            sa.Column("weight", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("properties", sa.JSON(), nullable=True),
            sa.Column("source_docs", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_graph_relations_tenant_id", "knowledge_graph_relations", ["tenant_id"])
        op.create_index("ix_knowledge_graph_relations_source_entity_id", "knowledge_graph_relations", ["source_entity_id"])
        op.create_index("ix_knowledge_graph_relations_target_entity_id", "knowledge_graph_relations", ["target_entity_id"])
        op.create_index("ix_kg_rel_tenant_source", "knowledge_graph_relations", ["tenant_id", "source_entity_id"])
        op.create_index("ix_kg_rel_tenant_target", "knowledge_graph_relations", ["tenant_id", "target_entity_id"])
        op.create_index("ix_kg_rel_tenant_type", "knowledge_graph_relations", ["tenant_id", "relation_type"])

    # ==================================================================
    # 45. rag_eval_results (models/rag_eval_models.py) -> rag_eval_tasks
    # ==================================================================
    if not _has_table(inspector, "rag_eval_results"):
        op.create_table(
            "rag_eval_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("rag_eval_tasks.id"), nullable=False),
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("retrieved_docs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("relevance_scores", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("precision_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("recall_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("mrr_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("ndcg_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("answer_traceback", sa.JSON(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_rag_eval_results_tenant_id", "rag_eval_results", ["tenant_id"])
        op.create_index("ix_rag_eval_results_task_id", "rag_eval_results", ["task_id"])
        op.create_index("ix_rag_eval_result_tenant_task", "rag_eval_results", ["tenant_id", "task_id"])

    # ==================================================================
    # 46. evaluation_results (models/evaluation_models.py)
    #     -> evaluation_tasks, dataset_items
    # ==================================================================
    if not _has_table(inspector, "evaluation_results"):
        op.create_table(
            "evaluation_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("evaluation_tasks.id"), nullable=False),
            sa.Column("dataset_item_id", sa.Integer(), sa.ForeignKey("dataset_items.id"), nullable=False),
            sa.Column("agent_output", sa.Text(), nullable=False),
            sa.Column("judge_scores", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("judge_feedback", sa.Text(), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_evaluation_results_tenant_id", "evaluation_results", ["tenant_id"])
        op.create_index("ix_evaluation_results_task_id", "evaluation_results", ["task_id"])
        op.create_index("ix_evaluation_results_dataset_item_id", "evaluation_results", ["dataset_item_id"])
        op.create_index("ix_eval_result_tenant_task", "evaluation_results", ["tenant_id", "task_id"])
        op.create_index("ix_eval_result_tenant_item", "evaluation_results", ["tenant_id", "dataset_item_id"])


def downgrade() -> None:
    """Downgrade schema: 按依赖逆序删除所有表."""
    for table in reversed(_NEW_TABLES):
        op.drop_table(table)
