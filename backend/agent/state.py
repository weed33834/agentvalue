"""
LangGraph Agent 状态定义
"""

from typing import Annotated, Any, Dict, List, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class EvaluationState(TypedDict):
    """评估工作流状态"""

    # 输入
    employee_id: str
    period: str
    raw_inputs: List[Dict[str, Any]]
    # 重新评估时携带的历史反馈/申诉(普通首轮评估为空)
    feedback: Optional[List[Dict[str, Any]]]

    # 上下文（由 Tools 填充）
    employee_history: Optional[List[Dict[str, Any]]]
    company_kb: Optional[List[Dict[str, Any]]]

    # 中间产物
    cleaned_inputs: Optional[List[Dict[str, Any]]]
    prompt: Optional[str]
    # P1 调试增强: prompt 版本信息 (DbPromptLoader 填充,文件 fallback 时为 None)
    # 用于 Langfuse trace 绑定,便于追溯某次评估用了哪个 prompt 版本
    prompt_version_info: Optional[Dict[str, Any]]

    # 模型输出
    llm_raw_output: Optional[str]
    parsed_evaluation: Optional[Dict[str, Any]]

    # 审批
    status: str  # raw_data -> ai_processing -> ai_drafted -> manager_review -> hr_audit -> approved / rejected
    manager_review_comment: Optional[str]
    hr_review_comment: Optional[str]

    # 错误与审计
    error: Optional[str]
    audit_info: Optional[Dict[str, Any]]

    # 兼容 langgraph 消息累加
    messages: Annotated[list, add_messages]
