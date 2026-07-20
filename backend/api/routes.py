"""
FastAPI 路由定义
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    AppState,
    assert_manager_team_access,
    get_app_state,
    get_approval_service,
    get_audit_service,
    get_evaluation_service,
)
from auth.rbac import Role, can_access, get_client_ip, get_current_user_id, require_role
from core.config import get_settings
from core.database import get_db
from core.guards import InputGuard
from core.multimodal.extractors import _validate_magic_bytes
from core.storage import AttachmentStorage, get_storage
from core.tenant_context import get_current_tenant
from core.job_queue import JobQueue, create_job_queue
from core.metrics import (
    record_approval_transition,
    record_evaluation,
    record_evaluation_failure,
    record_feedback,
)
from core.rate_limit import rate_limit
from core.tracing import tracer
from models.constants import EvaluationStatus
from models.models import DEFAULT_TENANT_ID, Tenant
from services.approval_service import ApprovalService
from services.audit_service import AuditService
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# 文本字段长度上限:防止超大 comment/content/reason 膨胀数据库与审计表
MAX_TEXT_FIELD_LENGTH = 5000


def _validate_text_field(value, field_name: str = "comment") -> str:
    """校验文本字段类型与长度,返回安全值(None 转空串)。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} 必须为字符串",
        )
    if len(value) > MAX_TEXT_FIELD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} 长度超限(最多 {MAX_TEXT_FIELD_LENGTH} 字符)",
        )
    return value


class CreateInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str = Field(min_length=1, max_length=64)
    period: str = Field(min_length=1, max_length=32)
    type: str = Field(default="daily_report", max_length=64)
    content: str = Field(min_length=1, max_length=10000)
    attachments: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)
    input_id: Optional[str] = Field(default=None, max_length=128)


class RawInputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: Optional[str] = Field(default=None, max_length=128)
    type: str = Field(default="daily_report", max_length=64)
    content: str = Field(min_length=1, max_length=10000)
    attachments: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)


class CreateEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str = Field(min_length=1, max_length=64)
    period: str = Field(min_length=1, max_length=32)
    raw_inputs: List[RawInputItem] = Field(default_factory=list, max_length=50)


# 异步评估任务队列:配置 redis_url 走 Redis(多实例),否则内存(测试/本地默认)
# job_store 作为向后兼容 shim 保留:conftest 同步调用 job_store.clear() 做测试间状态清理,
# 而 JobQueue.clear 是 async,这里包一层同步 clear 直清 InMemory 内部 dict
job_queue: JobQueue = create_job_queue(get_settings())


class _JobStoreCompat:
    """job_store 向后兼容 shim:仅服务于测试 conftest 的同步 .clear() 调用。
    业务代码应直接用 job_queue 的 async 接口。"""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def clear(self) -> None:
        store = getattr(self._queue, "_store", None)
        if isinstance(store, dict):
            store.clear()

    def __getattr__(self, name: str):
        # 其余属性透传给底层队列,保持向后引用兼容
        return getattr(self._queue, name)


job_store = _JobStoreCompat(job_queue)

# 输入护栏单例：拦截 Prompt 注入、恶意指令、超大输入与不支持的附件类型
_input_guard = InputGuard()


async def _update_job(job_id: str, update: Dict[str, Any]) -> None:
    """更新任务状态(浅合并 + 刷新 updated_at,语义对齐原 Dict 实现)"""
    await job_queue.update(job_id, update)


async def _run_evaluation_job(
    job_id: str,
    employee_id: str,
    period: str,
    raw_inputs: List[Dict[str, Any]],
    app_state: AppState,
    tenant_id: str = "default",
    actor_id: str = "system",
) -> None:
    """后台执行评估图，并更新 job_store。

    后台任务脱离请求上下文，需显式重设 tenant contextvar，确保落库与向量库
    写入归属到触发评估的租户。

    P1-8 修复: actor_id 由调用方传入(从请求的 JWT 解出),审计日志记录真实触发者,
    而非硬编码 "system"。同时 set_audit_context 让 audit_decorator 装饰的 service
    方法也能拿到正确 actor。
    """
    from core.database import AsyncSessionLocal
    from core.tenant_context import reset_current_tenant, set_current_tenant
    from services.audit_decorator import reset_audit_context, set_audit_context

    token = set_current_tenant(tenant_id)
    audit_token = set_audit_context(actor_id, None)
    try:
        async with AsyncSessionLocal() as session:
            eval_service = EvaluationService(session)
            audit_service = AuditService(session)
            graph = app_state.get_graph(eval_service, tenant_id=tenant_id)
            initial_state = {
                "employee_id": employee_id,
                "period": period,
                "raw_inputs": raw_inputs,
                "messages": [],
            }

            try:
                with tracer.trace(
                    name="create_evaluation_async",
                    evaluation_id=None,
                    employee_id=employee_id,
                    metadata={
                        "period": period,
                        "input_count": len(raw_inputs),
                        "job_id": job_id,
                        "actor_id": actor_id,
                    },
                ) as trace:
                    with tracer.span(trace, "run_graph", input_data=initial_state):
                        result = await graph.ainvoke(initial_state)

                    if result.get("error"):
                        trace.update(
                            metadata={**trace.metadata, "error": result["error"]}
                        )
                        logger.error(
                            "评估图执行失败 job_id=%s: %s", job_id, result["error"]
                        )
                        try:
                            record_evaluation_failure("graph_error")
                        except Exception:
                            logger.exception(
                                "record_evaluation_failure 埋点失败 reason=graph_error"
                            )
                        await _update_job(
                            job_id,
                            {
                                "status": "failed",
                                "error": "评估处理失败，请查看服务端日志",
                            },
                        )
                        return

                    evaluation = result.get("parsed_evaluation")
                    if evaluation:
                        await eval_service.create_evaluation(evaluation)
                        # 高风险自动路由：通过状态机将 ai_drafted → hr_audit
                        routing = result.get("status")
                        if routing == EvaluationStatus.HR_AUDIT:
                            approval_service = ApprovalService(session)
                            await approval_service.transition_status(
                                evaluation_id=evaluation["evaluation_id"],
                                action="request_hr_review",
                                actor_id=actor_id,
                                actor_role="system",
                                comment="自动路由：高风险评估（低分或关键风险标记）",
                            )
                            evaluation["status"] = EvaluationStatus.HR_AUDIT
                        memory_payload = {
                            "period": period,
                            "summary": evaluation.get("employee_view", {}).get(
                                "summary", ""
                            ),
                            "overall_score": evaluation.get("overall_score"),
                            "status": evaluation.get("status"),
                        }
                        # 向量/DB 记忆写入失败不应回滚主评估，独立捕获
                        try:
                            await eval_service.add_memory(employee_id, memory_payload)
                        except Exception:
                            logger.exception(
                                "DB 记忆写入失败 employee_id=%s period=%s",
                                employee_id,
                                period,
                            )
                        try:
                            await app_state.get_memory_store(tenant_id).add_memory(
                                employee_id, memory_payload
                            )
                        except Exception:
                            logger.exception(
                                "向量记忆写入失败 employee_id=%s period=%s",
                                employee_id,
                                period,
                            )
                        trace.update(
                            output=evaluation,
                            metadata={
                                **trace.metadata,
                                "model_tier": evaluation.get("audit", {}).get(
                                    "model_tier"
                                ),
                                "overall_score": evaluation.get("overall_score"),
                            },
                        )
                        await audit_service.log(
                            actor_id=actor_id,
                            action="create_evaluation_async",
                            evaluation_id=evaluation.get("evaluation_id"),
                            employee_id=employee_id,
                            details={
                                "period": period,
                                "model_tier": evaluation.get("audit", {}).get(
                                    "model_tier"
                                ),
                            },
                        )
                        await session.commit()
                        await _update_job(
                            job_id,
                            {
                                "status": "completed",
                                "evaluation": evaluation,
                            },
                        )
                    else:
                        try:
                            record_evaluation_failure("no_result")
                        except Exception:
                            logger.exception(
                                "record_evaluation_failure 埋点失败 reason=no_result"
                            )
                        await _update_job(
                            job_id, {"status": "failed", "error": "未生成评估结果"}
                        )
            except Exception as e:
                logger.exception("评估处理失败 job_id=%s", job_id)
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("评估失败回滚事务失败 job_id=%s", job_id)
                try:
                    record_evaluation_failure("exception")
                except Exception:
                    logger.exception(
                        "record_evaluation_failure 埋点失败 reason=exception"
                    )
                await _update_job(
                    job_id,
                    {"status": "failed", "error": "评估处理失败，请查看服务端日志"},
                )
    finally:
        reset_audit_context(audit_token)
        reset_current_tenant(token)


@router.post("/inputs", response_model=Dict[str, Any])
async def create_input(
    payload: CreateInputRequest,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """提交员工日报/任务进度等原始输入"""
    employee_id = payload.employee_id
    period = payload.period
    content = payload.content

    # 输入护栏：在写入数据库前拦截 Prompt 注入、恶意指令与超大附件
    # 计划书 11.2 要求所有输入入口接入输入护栏
    guard_result = _input_guard.check(
        [
            {
                "content": content,
                "attachments": payload.attachments or [],
            }
        ]
    )
    if not guard_result.allowed:
        # 拦截行为计入审计日志，便于安全运营追溯
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="input_blocked",
            employee_id=employee_id,
            details={
                "period": period,
                "reason": guard_result.reason,
                "triggered_rules": guard_result.triggered_rules,
            },
            ip_address=get_client_ip(request),
        )
        # P1-5：护栏检查结构化审计记录，便于区分"真拦截"与"误报"
        await audit_service.record_guard_check(
            guard_type="input",
            result="blocked",
            triggered_rules=guard_result.triggered_rules,
            would_be_false_positive=getattr(
                guard_result, "would_be_false_positive", False
            ),
            employee_id=employee_id,
            ip_address=get_client_ip(request),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )

    # employee 角色只能为自己提交输入
    if role == Role.EMPLOYEE:
        employee_id = await get_current_user_id(request)

    input_id = payload.input_id or f"input-{uuid.uuid4().hex[:8]}"
    raw = await eval_service.create_raw_input(
        {
            "input_id": input_id,
            "employee_id": employee_id,
            "period": period,
            "type": payload.type,
            "content": content,
            "attachments": payload.attachments or [],
        }
    )

    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="create_input",
        employee_id=employee_id,
        details={"input_id": input_id, "period": period, "type": raw.type},
        ip_address=get_client_ip(request),
    )
    await session.commit()

    return {
        "input_id": raw.input_id,
        "employee_id": raw.employee_id,
        "period": raw.period,
        "type": raw.type,
        "content": raw.content,
        "created_at": raw.created_at.isoformat(),
    }


@router.get("/inputs", response_model=Dict[str, Any])
async def list_inputs(
    request: Request,
    employee_id: Optional[str] = None,
    period: Optional[str] = None,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询原始输入列表"""
    current_user_id = await get_current_user_id(request)
    # employee 只能查看自己的输入
    if role == Role.EMPLOYEE:
        employee_id = current_user_id
    # H7：manager 传了 employee_id 时校验该员工是否为直属下属
    if role == Role.MANAGER and employee_id:
        await assert_manager_team_access(
            eval_service, role, employee_id, current_user_id
        )
    inputs = await eval_service.list_raw_inputs(employee_id=employee_id, period=period)
    return {
        "inputs": [
            {
                "input_id": i.input_id,
                "employee_id": i.employee_id,
                "period": i.period,
                "type": i.type,
                "content": i.content,
                "created_at": i.created_at.isoformat(),
            }
            for i in inputs
        ],
        "count": len(inputs),
    }


@router.get("/inputs/{input_id}")
async def get_input(
    input_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询原始输入"""
    raw = await eval_service.get_raw_input(input_id)
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="输入不存在")
    current_user_id = await get_current_user_id(request)
    # employee 只能查看自己的输入
    if role == Role.EMPLOYEE:
        if raw.employee_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该输入"
            )
    # H7：manager 仅能查看直属下属的输入
    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, raw.employee_id, current_user_id
        )
    return {
        "input_id": raw.input_id,
        "employee_id": raw.employee_id,
        "period": raw.period,
        "type": raw.type,
        "content": raw.content,
        "attachments": raw.attachments,
        "created_at": raw.created_at.isoformat(),
    }


def get_attachment_storage() -> AttachmentStorage:
    """附件存储依赖：返回进程级单例(按配置自动选择本地/S3)。"""
    return get_storage()


@router.post("/attachments", response_model=Dict[str, Any])
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    storage: AttachmentStorage = Depends(get_attachment_storage),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """上传附件到对象存储，返回 url 供后续 /inputs 引用。

    复用 InputGuard 的附件类型/大小校验，保持与 inputs 端点一致的拦截规则。
    存储后端由配置决定：未配 S3 落本地目录，配了且连通则走 MinIO。
    """
    filename = file.filename or "upload"
    data = await file.read()
    size = len(data)
    mime = file.content_type or ""

    guard_result = _input_guard.check_attachment(
        filename=filename, size=size, mime=mime
    )
    if not guard_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=guard_result.reason,
        )

    # P1-12：入口侧 magic bytes 校验，防止伪装扩展名的恶意文件进入存储
    # （OCR/ASR 流程内已校验，但存储入口未拦截会导致脏文件落盘）
    ext_lower = filename.lower()[filename.rfind(".") :]
    if ext_lower in (".png", ".jpg", ".jpeg", ".webp"):
        expected_types = ["png", "jpeg", "webp"]
        if not _validate_magic_bytes(data[:32], expected_types):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"文件 {filename} 的 magic bytes 与图片类型不匹配 "
                    f"(expected one of {expected_types}, mime={mime})"
                ),
            )
    elif ext_lower in (".mp3", ".wav", ".mp4", ".m4a"):
        expected_types = ["mp3", "wav", "mp4", "m4a"]
        if not _validate_magic_bytes(data[:32], expected_types):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"文件 {filename} 的 magic bytes 与音视频类型不匹配 "
                    f"(expected one of {expected_types}, mime={mime})"
                ),
            )
    elif ext_lower == ".pdf":
        # _validate_magic_bytes 不支持 PDF，单独校验 %PDF- 前缀
        if not data[:5].startswith(b"%PDF-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"文件 {filename} 的 magic bytes 与 PDF 类型不匹配 (mime={mime})",
            )
    # 其他类型 (txt/md 等) 跳过 magic bytes 校验，仅大小+扩展名校验

    # 存储 key：日期目录 + uuid + 原扩展名，避免碰撞与可读性
    ext = filename[filename.rfind(".") :] if "." in filename else ""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"{today}/{uuid.uuid4().hex}{ext}"
    url = storage.upload(key, data, mime)

    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="upload_attachment",
        details={"filename": filename, "size": size, "key": key, "mime": mime},
        ip_address=get_client_ip(request),
    )
    await session.commit()

    return {
        "key": key,
        "url": url,
        "filename": filename,
        "size": size,
        "mime": mime,
    }


@router.post("/evaluations", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def create_evaluation(
    payload: CreateEvaluationRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """异步触发一次员工评估，立即返回 job_id"""
    employee_id = payload.employee_id
    period = payload.period
    raw_inputs = [inp.model_dump() for inp in payload.raw_inputs]

    # employee 角色只能为自己创建评估
    if role == Role.EMPLOYEE:
        employee_id = await get_current_user_id(request)

    # 确保用户存在
    await eval_service.ensure_user_exists(employee_id, role="employee")

    # H8：持久化前先过输入护栏，拦截 Prompt 注入/恶意指令，
    # 避免 raw_inputs 落库后才被图内护栏发现导致脏数据
    guard_result = _input_guard.check(
        [
            {
                "content": inp.get("content", ""),
                "attachments": inp.get("attachments", []),
            }
            for inp in raw_inputs
        ]
        or [{"content": "占位", "attachments": []}]
    )
    if not guard_result.allowed:
        # P1-5：护栏检查结构化审计记录，便于区分"真拦截"与"误报"
        await audit_service.record_guard_check(
            guard_type="input",
            result="blocked",
            triggered_rules=guard_result.triggered_rules,
            would_be_false_positive=getattr(
                guard_result, "would_be_false_positive", False
            ),
            employee_id=employee_id,
            ip_address=get_client_ip(request),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )

    # H9：若评估周期表有数据，则校验周期存在且处于 open 状态；
    # 周期表为空时跳过校验，保持向后兼容
    existing_period = await eval_service.get_period(period)
    if existing_period is not None and existing_period.status != "open":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"评估周期 {period} 已关闭或不存在，无法创建评估",
        )

    # 持久化传入的 raw_inputs（如果尚未存在）
    for inp in raw_inputs:
        existing = await eval_service.get_raw_input(inp.get("input_id"))
        if not existing:
            await eval_service.create_raw_input(
                {
                    "input_id": inp.get("input_id") or f"input-{uuid.uuid4().hex[:8]}",
                    "employee_id": employee_id,
                    "period": period,
                    "type": inp.get("type", "daily_report"),
                    "content": inp.get("content", ""),
                    "attachments": inp.get("attachments", []),
                }
            )

    # 如果没有传入 raw_inputs，从数据库拉取
    if not raw_inputs:
        inputs = await eval_service.list_raw_inputs(
            employee_id=employee_id, period=period
        )
        raw_inputs = [
            {"input_id": i.input_id, "type": i.type, "content": i.content}
            for i in inputs
        ]

    await session.commit()

    job_id = f"job-{uuid.uuid4().hex[:12]}"
    await job_queue.enqueue(
        job_id,
        {
            "job_id": job_id,
            "status": "pending",
            "employee_id": employee_id,
            "period": period,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    # 后台任务脱离请求上下文，这里先捕获当前租户/触发者，避免 contextvar 丢失
    tenant_id = get_current_tenant()
    actor_id = await get_current_user_id(request)
    background_tasks.add_task(
        _run_evaluation_job,
        job_id,
        employee_id,
        period,
        raw_inputs,
        app_state,
        tenant_id,
        actor_id,
    )

    return {"job_id": job_id, "status": "pending"}


@router.get("/evaluations/jobs/{job_id}")
async def get_evaluation_job(
    job_id: str,
    request: Request,
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """查询异步评估任务状态"""
    job = await job_queue.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    current_user_id = await get_current_user_id(request)
    # employee 只能查看自己的任务
    if role == Role.EMPLOYEE:
        if job.get("employee_id") != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务"
            )
    # H7：manager 仅能查看直属下属的任务
    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, job.get("employee_id", ""), current_user_id
        )
    return job


@router.get("/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str,
    request: Request,
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """获取评估结果，按角色过滤可见字段"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    current_user_id = await get_current_user_id(request)
    if role == Role.EMPLOYEE:
        if evaluation.employee_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该评估"
            )

    # H7：manager 仅能查看直属下属的 manager_view / audit 字段，与 get_manager_view 保持一致
    if role == Role.MANAGER and (
        can_access(role, "manager_view") or can_access(role, "audit")
    ):
        await assert_manager_team_access(
            eval_service, role, evaluation.employee_id, current_user_id
        )

    data = {
        "evaluation_id": evaluation.evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "overall_score": evaluation.overall_score,
        "status": evaluation.status,
        "created_at": evaluation.created_at.isoformat(),
        "approved_at": (
            evaluation.approved_at.isoformat() if evaluation.approved_at else None
        ),
        "approver_id": evaluation.approver_id,
    }

    if can_access(role, "employee_view"):
        data["employee_view"] = evaluation.employee_view
    if can_access(role, "manager_view"):
        data["manager_view"] = evaluation.manager_view
    if can_access(role, "audit"):
        data["audit"] = evaluation.audit

    return data


@router.get("/evaluations/{evaluation_id}/employee-view")
async def get_employee_view(
    evaluation_id: str,
    request: Request,
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
    eval_service: EvaluationService = Depends(get_evaluation_service),
):
    """员工可见视图"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    if role == Role.EMPLOYEE:
        current_user_id = await get_current_user_id(request)
        if evaluation.employee_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该评估"
            )
    return {
        "evaluation_id": evaluation.evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "employee_view": evaluation.employee_view,
    }


@router.get("/evaluations/{evaluation_id}/manager-view")
async def get_manager_view(
    evaluation_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """管理/HR 可见视图"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")
    # H7：manager 仅能查看直属下属的管理视图
    await assert_manager_team_access(
        eval_service, role, evaluation.employee_id, await get_current_user_id(request)
    )
    # 管理视图查看行为计入审计日志（计划书 11.4：所有查看行为计入审计日志）
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="view_manager_view",
        evaluation_id=evaluation_id,
        employee_id=evaluation.employee_id,
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "evaluation_id": evaluation.evaluation_id,
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "manager_view": evaluation.manager_view,
    }


@router.post("/evaluations/{evaluation_id}/approve")
@rate_limit("30/minute")
async def approve_evaluation(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    approval_service: ApprovalService = Depends(get_approval_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """审批通过评估"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    # H7：manager 仅能审批直属下属的评估
    await assert_manager_team_access(
        eval_service, role, evaluation.employee_id, actor_id
    )

    current_status = evaluation.status
    comment = payload.get("comment")

    try:
        current_status, new_status = await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="approve",
            actor_id=actor_id,
            actor_role=role.value,
            comment=comment,
            approver_id=actor_id,
        )
        await audit_service.log(
            actor_id=actor_id,
            action="approve_evaluation",
            evaluation_id=evaluation_id,
            details={"from_status": current_status, "to_status": new_status},
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {"evaluation_id": evaluation_id, "status": new_status}
    except ValueError as e:
        # P1-8：状态机非法转移应计入审计，便于追踪审批失败链路
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="approve_failed",
            evaluation_id=evaluation_id,
            details={"reason": str(e)[:200], "from_status": current_status},
            ip_address=get_client_ip(request),
        )
        await session.rollback()
        logger.debug("approve 状态机拒绝 eval=%s: %s", evaluation_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/evaluations/{evaluation_id}/reject")
@rate_limit("30/minute")
async def reject_evaluation(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    approval_service: ApprovalService = Depends(get_approval_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """驳回评估"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    # H7：manager 仅能驳回直属下属的评估
    await assert_manager_team_access(
        eval_service, role, evaluation.employee_id, actor_id
    )

    current_status = evaluation.status
    comment = payload.get("comment")

    try:
        current_status, new_status = await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="reject",
            actor_id=actor_id,
            actor_role=role.value,
            comment=comment,
        )
        await audit_service.log(
            actor_id=actor_id,
            action="reject_evaluation",
            evaluation_id=evaluation_id,
            details={
                "from_status": current_status,
                "to_status": new_status,
                "comment": comment,
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {"evaluation_id": evaluation_id, "status": new_status}
    except ValueError as e:
        # P1-8：状态机非法转移应计入审计
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="reject_failed",
            evaluation_id=evaluation_id,
            details={"reason": str(e)[:200], "from_status": current_status},
            ip_address=get_client_ip(request),
        )
        await session.rollback()
        logger.debug("reject 状态机拒绝 eval=%s: %s", evaluation_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/evaluations/{evaluation_id}/feedback")
@rate_limit("30/minute")
async def create_feedback(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    app_state: AppState = Depends(get_app_state),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """员工反馈/申诉"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    if role == Role.EMPLOYEE:
        current_user_id = await get_current_user_id(request)
        if evaluation.employee_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该评估"
            )

    content = _validate_text_field(payload.get("content"), "content")
    feedback_type = payload.get("type", "feedback")
    # 归一化到已知类型,避免 Prometheus label 基数爆炸
    if feedback_type not in ("feedback", "appeal", "hr_request_more_info"):
        feedback_type = "other"
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content 必填",
        )

    # P1-10：反馈内容入口接入输入护栏，拦截 Prompt 注入/恶意指令
    guard_result = _input_guard.check([{"content": str(content)}])
    if not guard_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )

    actor_id = await get_current_user_id(request)
    feedback_id = payload.get("feedback_id") or f"FB-{uuid.uuid4().hex[:8]}"
    feedback = await eval_service.create_feedback(
        {
            "feedback_id": feedback_id,
            "evaluation_id": evaluation_id,
            "employee_id": evaluation.employee_id,
            "type": feedback_type,
            "content": content,
        }
    )

    await audit_service.log(
        actor_id=actor_id,
        action="create_feedback",
        evaluation_id=evaluation_id,
        employee_id=evaluation.employee_id,
        details={"feedback_id": feedback_id, "type": feedback_type},
        ip_address=get_client_ip(request),
    )
    await session.commit()

    # 员工主动反馈(非申诉/HR 要求补充)写入向量记忆,让下次评估 retrieve_context 能检索到
    if feedback_type == "feedback":
        tenant_id = get_current_tenant()
        feedback_memory = {
            "period": f"feedback-{evaluation.period}",
            "summary": f"员工反馈({evaluation.period}): {content}",
            "type": "feedback",
            "content": content,
            "evaluation_id": evaluation_id,
        }
        try:
            await app_state.get_memory_store(tenant_id).add_memory(
                evaluation.employee_id, feedback_memory
            )
        except Exception:
            logger.exception(
                "反馈向量记忆写入失败 employee_id=%s period=%s",
                evaluation.employee_id,
                evaluation.period,
            )

    # 业务埋点:反馈量按类型计数,失败不影响主流程
    try:
        record_feedback(feedback_type)
    except Exception:
        logger.exception("record_feedback 埋点失败 type=%s", feedback_type)

    return {
        "feedback_id": feedback.feedback_id,
        "evaluation_id": feedback.evaluation_id,
        "type": feedback.type,
        "content": feedback.content,
        "created_at": feedback.created_at.isoformat(),
    }


@router.get("/evaluations/{evaluation_id}/audit-logs")
async def get_evaluation_audit_logs(
    evaluation_id: str,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """获取评估审计日志"""
    # H7：manager 仅能查看直属下属评估的审计日志
    if role == Role.MANAGER:
        evaluation = await eval_service.get_evaluation(evaluation_id)
        if not evaluation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在"
            )
        await assert_manager_team_access(
            eval_service,
            role,
            evaluation.employee_id,
            await get_current_user_id(request),
        )
    logs = await audit_service.get_logs(evaluation_id=evaluation_id)
    return {
        "evaluation_id": evaluation_id,
        "logs": [
            {
                "log_id": log.log_id,
                "actor_id": log.actor_id,
                "action": log.action,
                "details": log.details,
                "ip_address": log.ip_address,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }


def _serialize_feedback_row(feedback, evaluation) -> Dict[str, Any]:
    """序列化反馈记录及其关联评估的当前状态，供前端追踪申诉处理进度"""
    return {
        "feedback_id": feedback.feedback_id,
        "evaluation_id": feedback.evaluation_id,
        "employee_id": feedback.employee_id,
        "type": feedback.type,
        "content": feedback.content,
        "created_at": feedback.created_at.isoformat(),
        "evaluation": {
            "period": evaluation.period,
            "overall_score": evaluation.overall_score,
            "status": evaluation.status,
            "created_at": evaluation.created_at.isoformat(),
            "approved_at": (
                evaluation.approved_at.isoformat() if evaluation.approved_at else None
            ),
        },
    }


@router.get("/evaluations/{evaluation_id}/feedback")
async def list_evaluation_feedback(
    evaluation_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询某评估下的反馈/申诉记录（员工仅可查自己的评估）"""
    rows = await eval_service.list_feedback(evaluation_id=evaluation_id, limit=200)
    # employee 仅能查看本人评估的反馈
    if role == Role.EMPLOYEE:
        current_user_id = await get_current_user_id(request)
        rows = [r for r in rows if r[0].employee_id == current_user_id]
    return {
        "evaluation_id": evaluation_id,
        "feedback": [_serialize_feedback_row(fb, ev) for fb, ev in rows],
        "count": len(rows),
    }


@router.get("/employees/{employee_id}/feedback")
async def list_employee_feedback(
    employee_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询员工的反馈/申诉记录及其关联评估当前状态，用于追踪申诉处理进度"""
    if role == Role.EMPLOYEE:
        employee_id = await get_current_user_id(request)
    rows = await eval_service.list_feedback(employee_id=employee_id, limit=200)
    return {
        "employee_id": employee_id,
        "feedback": [_serialize_feedback_row(fb, ev) for fb, ev in rows],
        "count": len(rows),
    }


@router.get("/manager/pending-approvals")
async def get_pending_approvals(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """主管待审批列表（包含 ai_drafted 与 manager_review），分页返回"""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
    # H7：manager 仅看到直属下属的待审批；HR/ADMIN 不受限
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
    else:
        report_ids = None
    pending = []
    total = 0
    for status in (EvaluationStatus.AI_DRAFTED, EvaluationStatus.MANAGER_REVIEW):
        result = await eval_service.list_evaluations(status=status, limit=200)
        items = result["items"]
        if report_ids is not None:
            items = [e for e in items if e.employee_id in report_ids]
        pending.extend(items)
        # total 反映过滤后实际可见数量，便于前端分页正确
        total += len(items)
    # 按创建时间倒序后分页
    pending.sort(key=lambda e: e.created_at, reverse=True)
    offset = (page - 1) * page_size
    page_items = pending[offset : offset + page_size]
    return {
        "pending": [
            {
                "evaluation_id": e.evaluation_id,
                "employee_id": e.employee_id,
                "period": e.period,
                "status": e.status,
                "overall_score": e.overall_score,
                "created_at": e.created_at.isoformat(),
            }
            for e in page_items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/manager/dashboard")
async def get_manager_dashboard(
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """主管工作台概览"""
    # H7：manager 仅看到直属下属的数据；HR/ADMIN 不受限
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
    else:
        report_ids = None
    pending = []
    for status in (EvaluationStatus.AI_DRAFTED, EvaluationStatus.MANAGER_REVIEW):
        items = (await eval_service.list_evaluations(status=status, limit=200))["items"]
        if report_ids is not None:
            items = [e for e in items if e.employee_id in report_ids]
        pending.extend(items)
    approved_items = (
        await eval_service.list_evaluations(status=EvaluationStatus.APPROVED, limit=10)
    )["items"]
    if report_ids is not None:
        approved_items = [e for e in approved_items if e.employee_id in report_ids]
    return {
        "pending_count": len(pending),
        "pending": [
            {
                "evaluation_id": e.evaluation_id,
                "employee_id": e.employee_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "created_at": e.created_at.isoformat(),
            }
            for e in pending[:10]
        ],
        "recent_approved": [
            {
                "evaluation_id": e.evaluation_id,
                "employee_id": e.employee_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "approved_at": e.approved_at.isoformat() if e.approved_at else None,
            }
            for e in approved_items[:5]
        ],
    }


@router.get("/hr/audit-queue")
async def get_hr_audit_queue(
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """HR 复核队列"""
    audits = (
        await eval_service.list_evaluations(status=EvaluationStatus.HR_AUDIT, limit=200)
    )["items"]
    return {
        "pending": [
            {
                "evaluation_id": e.evaluation_id,
                "employee_id": e.employee_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "created_at": e.created_at.isoformat(),
            }
            for e in audits
        ]
    }


@router.post("/evaluations/{evaluation_id}/request-hr-review")
async def request_hr_review(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    approval_service: ApprovalService = Depends(get_approval_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """主管提交 HR 复核"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    # H7：manager 仅能对直属下属的评估申请 HR 复核
    await assert_manager_team_access(
        eval_service, role, evaluation.employee_id, actor_id
    )

    current_status = evaluation.status
    comment = payload.get("comment")

    try:
        current_status, new_status = await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="request_hr_review",
            actor_id=actor_id,
            actor_role=role.value,
            comment=comment,
        )
        await audit_service.log(
            actor_id=actor_id,
            action="request_hr_review",
            evaluation_id=evaluation_id,
            details={
                "from_status": current_status,
                "to_status": new_status,
                "comment": comment,
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {"evaluation_id": evaluation_id, "status": new_status}
    except ValueError as e:
        # P1-8：状态机非法转移应计入审计
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="request_hr_review_failed",
            evaluation_id=evaluation_id,
            details={"reason": str(e)[:200], "from_status": current_status},
            ip_address=get_client_ip(request),
        )
        await session.rollback()
        logger.debug("request_hr_review 状态机拒绝 eval=%s: %s", evaluation_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/evaluations/{evaluation_id}/require-reeval")
async def require_reeval(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    app_state: AppState = Depends(get_app_state),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    approval_service: ApprovalService = Depends(get_approval_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """M2：HR 复核时退回重评，评估回到 ai_drafted 并自动触发后台重新评估"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    actor_id = await get_current_user_id(request)
    comment = payload.get("comment")
    employee_id = evaluation.employee_id
    period = evaluation.period
    current_status = evaluation.status

    try:
        current_status, new_status = await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="require_reeval",
            actor_id=actor_id,
            actor_role=role.value,
            comment=comment,
        )
        await audit_service.log(
            actor_id=actor_id,
            action="require_reeval",
            evaluation_id=evaluation_id,
            details={
                "from_status": current_status,
                "to_status": new_status,
                "comment": comment,
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
    except ValueError as e:
        # P1-8：状态机非法转移应计入审计
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="require_reeval_failed",
            evaluation_id=evaluation_id,
            details={"reason": str(e)[:200], "from_status": current_status},
            ip_address=get_client_ip(request),
        )
        await session.rollback()
        logger.debug("require_reeval 状态机拒绝 eval=%s: %s", evaluation_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 自动触发后台重新评估：拉取该周期原始输入，复用 _run_evaluation_job
    raw_inputs = [
        {"input_id": i.input_id, "type": i.type, "content": i.content}
        for i in await eval_service.list_raw_inputs(
            employee_id=employee_id, period=period
        )
    ]
    if raw_inputs:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        await job_queue.enqueue(
            job_id,
            {
                "job_id": job_id,
                "status": "pending",
                "employee_id": employee_id,
                "period": period,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        tenant_id = get_current_tenant()
        background_tasks.add_task(
            _run_evaluation_job,
            job_id,
            employee_id,
            period,
            raw_inputs,
            app_state,
            tenant_id,
            actor_id,
        )

    return {"evaluation_id": evaluation_id, "status": new_status}


@router.post("/evaluations/{evaluation_id}/appeal")
@rate_limit("30/minute")
async def appeal_evaluation(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    approval_service: ApprovalService = Depends(get_approval_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """员工对 approved/rejected 评估提出申诉，回到 manager_review"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    current_status = evaluation.status
    actor_id = await get_current_user_id(request)
    comment = payload.get("comment")

    if current_status not in (EvaluationStatus.APPROVED, EvaluationStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有 approved 或 rejected 状态的评估可以申诉",
        )

    # P1-10：申诉理由入口接入输入护栏，拦截 Prompt 注入/恶意指令
    # comment 缺省时使用占位文案作为护栏输入，与落库的 fallback 文案一致
    appeal_content = comment or "员工提出申诉"
    guard_result = _input_guard.check([{"content": str(appeal_content)}])
    if not guard_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )

    try:
        current_status, new_status = await approval_service.transition_status(
            evaluation_id=evaluation_id,
            action="appeal",
            actor_id=actor_id,
            actor_role=role.value,
            comment=comment,
        )
        # 申诉写入 Feedback 表（type=appeal），使员工反馈面板可追踪申诉处理进度
        await eval_service.create_feedback(
            {
                "feedback_id": f"FB-{uuid.uuid4().hex[:8]}",
                "evaluation_id": evaluation_id,
                "employee_id": evaluation.employee_id,
                "type": "appeal",
                "content": comment or "员工提出申诉",
            }
        )
        await audit_service.log(
            actor_id=actor_id,
            action="appeal_evaluation",
            evaluation_id=evaluation_id,
            details={
                "from_status": current_status,
                "to_status": new_status,
                "comment": comment,
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        # 业务埋点:申诉量计数
        try:
            record_feedback("appeal")
        except Exception:
            logger.exception("record_feedback 埋点失败 type=appeal")
        return {"evaluation_id": evaluation_id, "status": new_status}
    except ValueError as e:
        # P1-8：状态机非法转移应计入审计
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="appeal_failed",
            evaluation_id=evaluation_id,
            details={"reason": str(e)[:200], "from_status": current_status},
            ip_address=get_client_ip(request),
        )
        await session.rollback()
        logger.debug("appeal 状态机拒绝 eval=%s: %s", evaluation_id, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        logger.exception("申诉落库失败 evaluation_id=%s", evaluation_id)
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="申诉处理失败",
        )


@router.post("/evaluations/{evaluation_id}/re-evaluate")
@rate_limit("10/minute")
async def re_evaluate(
    evaluation_id: str,
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """基于反馈或申诉重新运行评估，生成新的 AI 草稿"""
    evaluation = await eval_service.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评估不存在")

    # 收集原始输入与反馈
    raw_inputs = [
        {"input_id": i.input_id, "type": i.type, "content": i.content}
        for i in await eval_service.list_raw_inputs(
            employee_id=evaluation.employee_id, period=evaluation.period
        )
    ]
    if not raw_inputs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未找到该周期的原始输入，无法重新评估",
        )

    feedback_items = payload.get("feedback", [])
    if not isinstance(feedback_items, list):
        feedback_items = [feedback_items]

    # 拉取该评估历史已落库的反馈/申诉记录，合并调用方本次传入的反馈，
    # 注入评估图作为重新评估参考上下文，让申诉内容真正影响重评结果。
    existing_pairs = await eval_service.list_feedback(
        employee_id=evaluation.employee_id,
        evaluation_id=evaluation_id,
        limit=200,
    )
    historical_feedback = [
        {"type": fb.type, "content": fb.content} for fb, _ in existing_pairs
    ]
    # 调用方传入的 feedback 兼容两种形态：dict 或纯字符串
    caller_feedback: List[Dict[str, Any]] = []
    for item in feedback_items:
        if isinstance(item, dict):
            caller_feedback.append(
                {
                    "type": str(item.get("type", "feedback")),
                    "content": str(item.get("content", "")),
                }
            )
        elif isinstance(item, str) and item.strip():
            caller_feedback.append({"type": "feedback", "content": item})
    merged_feedback = historical_feedback + caller_feedback

    # P1-10：调用方传入的 feedback 入口接入输入护栏，拦截 Prompt 注入/恶意指令。
    # 历史 feedback 落库前已过护栏，不再重复校验；caller_feedback 为本次外部输入。
    if caller_feedback:
        guard_result = _input_guard.check(
            [{"content": str(fb.get("content", ""))} for fb in caller_feedback]
        )
        if not guard_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"输入被拦截: {guard_result.reason}",
            )

    graph = app_state.get_graph(eval_service)
    initial_state = {
        "employee_id": evaluation.employee_id,
        "period": evaluation.period,
        "raw_inputs": raw_inputs,
        "feedback": merged_feedback,
        "messages": [],
    }

    try:
        # H3：重新评估包一层 trace，便于在 Langfuse 中按 evaluation_id 检索重评链路
        with tracer.trace(
            name="re_evaluate",
            evaluation_id=evaluation_id,
            employee_id=evaluation.employee_id,
            metadata={
                "period": evaluation.period,
                "previous_status": evaluation.status,
                "feedback_count": len(feedback_items),
            },
        ) as trace:
            with tracer.span(trace, "run_graph", input_data=initial_state):
                result = await graph.ainvoke(initial_state)
    except Exception:
        logger.exception("重新评估图执行失败 evaluation_id=%s", evaluation_id)
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="评估处理失败",
        )

    if result.get("error"):
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="评估处理失败",
        )

    new_eval = result.get("parsed_evaluation")
    if not new_eval:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="重新评估未返回结果",
        )

    # 覆盖旧 evaluation_id，保留历史审计
    new_eval["evaluation_id"] = evaluation_id
    try:
        await eval_service.update_evaluation(
            evaluation_id=evaluation_id, evaluation_data=new_eval
        )

        # 高风险自动路由：通过状态机将 ai_drafted → hr_audit
        routing = result.get("status")
        if routing == EvaluationStatus.HR_AUDIT:
            approval_service = ApprovalService(session)
            await approval_service.transition_status(
                evaluation_id=evaluation_id,
                action="request_hr_review",
                actor_id="system",
                actor_role="system",
                comment="重新评估自动路由：高风险评估（低分或关键风险标记）",
            )
            new_eval["status"] = EvaluationStatus.HR_AUDIT

        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="re_evaluate",
            evaluation_id=evaluation_id,
            details={
                "previous_status": evaluation.status,
                "new_status": new_eval["status"],
                "feedback_count": len(merged_feedback),
                "historical_feedback_count": len(historical_feedback),
                "caller_feedback_count": len(caller_feedback),
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        logger.exception("重新评估落库失败 evaluation_id=%s", evaluation_id)
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="重新评估落库失败",
        )

    return {
        "evaluation_id": evaluation_id,
        "status": new_eval["status"],
        "feedback_processed": len(merged_feedback),
        "historical_feedback_count": len(historical_feedback),
        "caller_feedback_count": len(caller_feedback),
    }


@router.get("/employees/{employee_id}/dashboard")
async def get_employee_dashboard(
    employee_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """员工个人成长看板"""
    current_user_id = await get_current_user_id(request)
    if role == Role.EMPLOYEE:
        employee_id = current_user_id
    # H7：manager 仅能查看直属下属的看板
    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, employee_id, current_user_id
        )
    evaluations = (
        await eval_service.list_evaluations(
            employee_id=employee_id, status=EvaluationStatus.APPROVED, limit=10
        )
    )["items"]
    latest = evaluations[0] if evaluations else None
    return {
        "employee_id": employee_id,
        "latest_evaluation": (
            {
                "evaluation_id": latest.evaluation_id,
                "period": latest.period,
                "overall_score": latest.overall_score,
                "employee_view": latest.employee_view,
            }
            if latest
            else None
        ),
        "history_count": len(evaluations),
        "evaluations": [
            {
                "evaluation_id": e.evaluation_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "status": e.status,
                "employee_view": e.employee_view,
                "manager_view": e.manager_view,
                "created_at": e.created_at.isoformat(),
            }
            for e in evaluations
        ],
    }


@router.get("/employees/{employee_id}/history")
async def get_employee_history(
    employee_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """跨周期能力演进"""
    current_user_id = await get_current_user_id(request)
    if role == Role.EMPLOYEE:
        employee_id = current_user_id
    # H7：manager 仅能查看直属下属的历史
    if role == Role.MANAGER:
        await assert_manager_team_access(
            eval_service, role, employee_id, current_user_id
        )
    evaluations = (
        await eval_service.list_evaluations(
            employee_id=employee_id, status=EvaluationStatus.APPROVED, limit=50
        )
    )["items"]
    return {
        "employee_id": employee_id,
        "evaluations": [
            {
                "evaluation_id": e.evaluation_id,
                "period": e.period,
                "overall_score": e.overall_score,
                "employee_view": {
                    "summary": e.employee_view.get("summary", ""),
                    "growth_areas": e.employee_view.get("growth_areas", []),
                },
                "created_at": e.created_at.isoformat(),
            }
            for e in evaluations
        ],
    }


@router.post("/teams/{team_id}/analytics")
async def get_team_analytics(
    team_id: str,
    request: Request,
    payload: Optional[Dict[str, Any]] = None,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """团队分析（管理端）
    请求体示例：{"members": ["E1001", "E1002", "E1003"]}
    H7：manager 仅能查询直属下属的团队分析；HR/ADMIN 不受限
    """
    payload = payload or {}
    members = payload.get("members", [])
    if not isinstance(members, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="members 必须是数组",
        )
    if not members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="members 列表必填",
        )
    if len(members) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="members 数量超限(最多 200)",
        )
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
        unknown = [m for m in members if m not in report_ids]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"无权查询非直属下属: {unknown}",
            )
    analytics = await eval_service.get_team_analytics(members)
    return {"team_id": team_id, **analytics}


@router.get("/teams/{team_id}/analytics")
async def get_team_analytics_get(
    team_id: str,
    request: Request,
    members: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """团队分析（GET 版本，members 以逗号分隔）
    示例：/teams/team-1/analytics?members=E1001,E1002
    H7：manager 仅能查询直属下属的团队分析；HR/ADMIN 不受限
    """
    member_list = [m.strip() for m in members.split(",") if m.strip()]
    if not member_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="members 参数必填",
        )
    if len(member_list) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="members 数量超限(最多 200)",
        )
    if role == Role.MANAGER:
        current_user_id = await get_current_user_id(request)
        reports = await eval_service.list_direct_reports(current_user_id)
        report_ids = {r.user_id for r in reports}
        unknown = [m for m in member_list if m not in report_ids]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"无权查询非直属下属: {unknown}",
            )
    analytics = await eval_service.get_team_analytics(member_list)
    return {"team_id": team_id, **analytics}


@router.get("/admin/model-status")
async def get_model_status(
    app_state: AppState = Depends(get_app_state),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """获取模型状态与推荐档位"""
    return app_state.model_router.hardware_report()


@router.post("/admin/model-switch")
@rate_limit("10/minute")
async def switch_model_tier(
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """手动切换模型档位"""
    tier = payload.get("tier")
    valid_tiers = ["auto", "L0", "L1", "L2", "L3"]
    if tier not in valid_tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier 必须是其中之一: {valid_tiers}",
        )
    reason = _validate_text_field(payload.get("reason"), "reason")
    async with app_state._settings_lock:
        old_tier = app_state.settings.model_tier
        app_state.settings.model_tier = tier
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="switch_model_tier",
        details={"from_tier": old_tier, "to_tier": tier, "reason": reason},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"tier": tier, "recommended": app_state.model_router.get_recommended_tier()}


# ---------------- LLM 配置管理（密钥 / base_url / 模型名 / 推理参数） ----------------
# 仅 ADMIN 可读写；敏感字段 GET 时 mask，PUT 时若回传 mask 占位符则跳过；
# 修改后立即生效（ModelRouter 从同一 settings 实例读取），并持久化到 .env.runtime。

# 敏感字段：GET 时 mask 返回，PUT 时若值含 *** 视为未修改占位符跳过
_LLM_SENSITIVE_FIELDS = frozenset(
    {
        "cloud_api_key",
        "openai_api_key",
        "local_api_key",
        "embedding_api_key",
        "ocr_cloud_api_key",
        "ocr_cloud_secret_key",
        "asr_cloud_api_key",
        # P2-2: Rerank API Key 同样按敏感字段处理(mask 回显, 空串/mask 占位符跳过)
        "rerank_api_key",
    }
)

# 允许通过 admin API 修改的字段白名单
# 刻意排除 jwt_secret_key / field_encryption_key / langfuse_secret_key / s3_* / cors_origins
# 等安全相关配置——这些只能通过环境变量/部署流程修改，不开放运行时 API
_LLM_EDITABLE_FIELDS = (
    # 档位
    "model_tier",
    # 核心聊天模型（云端）
    "cloud_api_key",
    "cloud_base_url",
    "cloud_model",
    # OpenAI 兼容（兜底）
    "openai_api_key",
    "openai_base_url",
    "openai_model",
    # 本地模型
    "local_base_url",
    "local_api_key",
    "local_model_l1",
    "local_model_l2",
    "local_model_l3",
    # Embedding
    "embedding_api_key",
    "embedding_base_url",
    "embedding_model",
    "embedding_dimensions",
    # Vision / OCR
    "vision_model",
    "ocr_provider",
    "ocr_lang",
    "ocr_cloud_provider",
    "ocr_cloud_secret_key",
    "ocr_cloud_api_key",
    "ocr_cloud_base_url",
    "ocr_cloud_model",
    # ASR
    "asr_provider",
    "whisper_model",
    "asr_cloud_api_key",
    "asr_cloud_base_url",
    "asr_cloud_model",
    # 通用推理参数
    "temperature",
    "max_tokens",
    "llm_request_timeout",
    # P2-2: Rerank Provider 抽象(对标 Dify Rerank)
    "rerank_provider",
    "rerank_api_key",
    "rerank_base_url",
    "rerank_model",
    "rerank_top_k",
)


def _mask_secret(value):
    """敏感字段 mask：保留首尾各 3 字符，中间用 *** 代替；过短直接 ***"""
    if not value or not isinstance(value, str):
        return value
    if len(value) <= 8:
        return "***"
    return value[:3] + "***" + value[-3:]


def _is_mask_placeholder(value) -> bool:
    """判断是否为 mask 占位符（前端未修改该敏感字段时原样回传）"""
    return isinstance(value, str) and "***" in value


def _persist_runtime_env(settings) -> None:
    """将当前 LLM 配置持久化到 .env.runtime（gitignored），重启后自动加载。

    仅持久化 _LLM_EDITABLE_FIELDS 中的字段。文件不存在时自动创建。
    """
    import os

    runtime_path = os.path.join(os.getcwd(), ".env.runtime")
    lines = []
    for field in _LLM_EDITABLE_FIELDS:
        val = getattr(settings, field, None)
        if val is None:
            continue
        env_key = field.upper()
        lines.append(f"{env_key}={val}")
    try:
        with open(runtime_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        logger.exception("持久化 .env.runtime 失败")


@router.get("/admin/llm-config")
async def get_llm_config(
    app_state: AppState = Depends(get_app_state),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """获取当前 LLM 配置（敏感字段 mask 返回，仅 admin 可见）"""
    s = app_state.settings
    result = {}
    for field in _LLM_EDITABLE_FIELDS:
        val = getattr(s, field, None)
        if field in _LLM_SENSITIVE_FIELDS and val:
            val = _mask_secret(val)
        result[field] = val
    return result


@router.put("/admin/llm-config")
@rate_limit("10/minute")
async def update_llm_config(
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """更新 LLM 配置（仅 admin）

    - 仅接受 _LLM_EDITABLE_FIELDS 白名单内字段
    - 敏感字段若传入 mask 占位符（含 ***）则跳过，不覆盖原值
    - 修改后立即生效（ModelRouter/EmbeddingClient 等均从同一 settings 实例读取）
    - 持久化到 .env.runtime（gitignored），重启后自动加载
    - 记入审计日志（仅记字段名，不记值）
    """
    changed: List[str] = []
    skipped: List[str] = []
    async with app_state._settings_lock:
        for field, new_val in payload.items():
            if field not in _LLM_EDITABLE_FIELDS:
                continue
            if new_val is None:
                continue
            # 敏感字段：传入 mask 占位符视为未修改，跳过
            if field in _LLM_SENSITIVE_FIELDS and _is_mask_placeholder(new_val):
                skipped.append(field)
                continue
            # 敏感字段：空字符串视为未修改，跳过。
            # 防止前端表单空输入意外清空已配置的 API Key；如需显式清除密钥，
            # 应通过专用清除接口或直接编辑 .env.runtime，不开放空串覆盖。
            if (
                field in _LLM_SENSITIVE_FIELDS
                and isinstance(new_val, str)
                and new_val == ""
            ):
                skipped.append(field)
                continue
            old_val = getattr(app_state.settings, field, None)
            if old_val != new_val:
                setattr(app_state.settings, field, new_val)
                changed.append(field)
    if changed:
        _persist_runtime_env(app_state.settings)
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="update_llm_config",
            details={"changed_fields": changed, "skipped": skipped},
            ip_address=get_client_ip(request),
        )
        await session.commit()
    return {
        "changed": changed,
        "skipped": skipped,
        "message": (
            f"已更新 {len(changed)} 项配置"
            + (f"，跳过 {len(skipped)} 项未变更敏感字段" if skipped else "")
        ),
    }


@router.post("/admin/llm-config/test")
@rate_limit("10/minute")
async def test_llm_connection(
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """测试当前 LLM 连接是否可用（调 health_check，仅 admin）

    返回各档位健康状态，供前端"测试连接"按钮调用。
    """
    results = {}
    for tier in ("L0", "L1", "L2", "L3"):
        try:
            provider = app_state.model_router.get_provider(tier)
            ok = await provider.health_check()
            results[tier] = {"healthy": ok, "model": provider.config.model_name}
        except Exception as e:
            results[tier] = {"healthy": False, "error": str(e)}
    # 凭证使用/外部调用留痕:记录测试的档位与各档健康状态
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="test_llm_connection",
        details={
            tier: results[tier].get("healthy") for tier in ("L0", "L1", "L2", "L3")
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return results


@router.get("/admin/audit-logs")
async def get_admin_audit_logs(
    request: Request,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    audit_service: AuditService = Depends(get_audit_service),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """管理端审计日志查询，支持按操作人、动作筛选与分页"""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 20
    return await audit_service.list_logs(
        actor_id=actor_id,
        action=action,
        page=page,
        page_size=page_size,
    )


# ---------------- 公司知识库 CRUD（H1） ----------------


class CreateKBDocRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20000)
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _serialize_kb_doc(doc) -> Dict[str, Any]:
    return {
        "kb_id": doc.kb_id,
        "title": doc.title,
        "content": doc.content,
        "metadata": doc.metadata_,
        "created_at": doc.created_at.isoformat(),
    }


@router.post("/kb", response_model=Dict[str, Any])
async def create_kb_doc(
    payload: CreateKBDocRequest,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """创建知识库文档（仅 HR/ADMIN）"""
    existing = await eval_service.get_kb_doc(payload.kb_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"知识库文档 kb_id={payload.kb_id} 已存在",
        )
    # P1-10：知识库内容会进向量库与 LLM 检索，入口接入输入护栏
    guard_result = _input_guard.check([{"content": str(payload.content)}])
    if not guard_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )
    doc = await eval_service.create_kb_doc(
        {
            "kb_id": payload.kb_id,
            "title": payload.title,
            "content": payload.content,
            "metadata": payload.metadata,
        }
    )
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="create_kb_doc",
        details={"kb_id": doc.kb_id, "title": doc.title},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_kb_doc(doc)


@router.get("/kb", response_model=Dict[str, Any])
async def list_kb_docs(
    page: int = 1,
    page_size: int = 20,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """分页查询知识库文档（所有角色可读）"""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 20
    result = await eval_service.list_kb_docs(page=page, page_size=page_size)
    return {
        "items": [_serialize_kb_doc(d) for d in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/kb/{kb_id}", response_model=Dict[str, Any])
async def get_kb_doc(
    kb_id: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询单条知识库文档"""
    doc = await eval_service.get_kb_doc(kb_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    return _serialize_kb_doc(doc)


@router.delete("/kb/{kb_id}")
async def delete_kb_doc(
    kb_id: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """删除知识库文档（仅 ADMIN）"""
    deleted = await eval_service.delete_kb_doc(kb_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库文档不存在"
        )
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="delete_kb_doc",
        details={"kb_id": kb_id},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"kb_id": kb_id, "deleted": True}


# ---------------- 评估周期管理（H9） ----------------


class CreatePeriodRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: str = Field(min_length=1, max_length=32)
    period_type: str = Field(default="weekly", max_length=16)
    start_date: str = Field(min_length=1, max_length=32)
    end_date: str = Field(min_length=1, max_length=32)
    status: str = Field(default="open", max_length=32)


@router.post("/periods", response_model=Dict[str, Any])
async def create_period(
    payload: CreatePeriodRequest,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """创建评估周期（仅 HR/ADMIN）"""
    existing = await eval_service.get_period(payload.period)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"周期 {payload.period} 已存在",
        )
    try:
        start_dt = datetime.fromisoformat(payload.start_date)
        end_dt = datetime.fromisoformat(payload.end_date)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"日期格式无效，需 ISO 8601: {e}",
        )
    if end_dt < start_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_date 不能早于 start_date",
        )
    period = await eval_service.create_period(
        {
            "period": payload.period,
            "period_type": payload.period_type,
            "start_date": start_dt,
            "end_date": end_dt,
            "status": payload.status,
        }
    )
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="create_period",
        details={"period": payload.period, "status": payload.status},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_period(period)


def _serialize_period(period) -> Dict[str, Any]:
    return {
        "period": period.period,
        "period_type": period.period_type,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "status": period.status,
        "created_at": period.created_at.isoformat(),
    }


@router.get("/periods", response_model=Dict[str, Any])
async def list_periods(
    status_filter: Optional[str] = None,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询评估周期列表，可按状态过滤"""
    periods = await eval_service.list_periods(status=status_filter)
    return {
        "items": [_serialize_period(p) for p in periods],
        "count": len(periods),
    }


@router.get("/periods/{period}", response_model=Dict[str, Any])
async def get_period(
    period: str,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """查询单个评估周期"""
    period_obj = await eval_service.get_period(period)
    if not period_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="周期不存在")
    return _serialize_period(period_obj)


@router.post("/periods/{period}/close")
async def close_period(
    period: str,
    request: Request,
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """关闭评估周期（仅 HR/ADMIN）"""
    period_obj = await eval_service.close_period(period)
    if not period_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="周期不存在")
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="close_period",
        details={"period": period},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_period(period_obj)


# ---------------- 水印防截图校验（Phase 9.3） ----------------
# 前端定期上报水印生效状态，后端记录到审计日志；
# visibilitychange 事件（切后台可能是截图工具）单独标记，便于截图溯源。
watermark_reports: List[Dict[str, Any]] = []
_MAX_WATERMARK_REPORTS = 200


class WatermarkVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = Field(description="水印是否可见")
    text_sample: Optional[str] = Field(default=None, max_length=128)
    density: Optional[str] = Field(default=None, max_length=32)
    visibility_event: Optional[str] = Field(default=None, max_length=64)


@router.post("/watermark/verify", response_model=Dict[str, Any])
async def verify_watermark(
    payload: WatermarkVerifyRequest,
    request: Request,
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """接收前端水印状态上报，记录审计日志用于截图溯源。

    - 正常心跳（visible=True 且无 visibility_event）：记录 watermark_heartbeat
    - 切后台等异常事件：记录 watermark_visibility_change，可能是截图工具触发
    """
    actor_id = await get_current_user_id(request)
    now_iso = datetime.now(timezone.utc).isoformat()

    # P1-10：text_sample 字段入口接入输入护栏，防止上报内容携带 Prompt 注入
    if payload.text_sample:
        guard_result = _input_guard.check([{"content": str(payload.text_sample)}])
        if not guard_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"输入被拦截: {guard_result.reason}",
            )

    # 进程内保留最近上报，便于运维快速排查（生产应落监控时序库）
    report = {
        "actor_id": actor_id,
        "visible": payload.visible,
        "density": payload.density,
        "visibility_event": payload.visibility_event,
        "reported_at": now_iso,
    }
    watermark_reports.append(report)
    if len(watermark_reports) > _MAX_WATERMARK_REPORTS:
        del watermark_reports[: len(watermark_reports) - _MAX_WATERMARK_REPORTS]

    # 切后台等异常事件单独标记，可能是截图工具，需进审计日志
    if payload.visibility_event or not payload.visible:
        await audit_service.log(
            actor_id=actor_id,
            action="watermark_visibility_change",
            details={
                "visible": payload.visible,
                "visibility_event": payload.visibility_event,
                "density": payload.density,
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {
            "status": "recorded",
            "event": "visibility_change",
            "reported_at": now_iso,
        }

    await audit_service.log(
        actor_id=actor_id,
        action="watermark_heartbeat",
        details={"visible": True, "density": payload.density},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {"status": "ok", "event": "heartbeat", "reported_at": now_iso}


# ---------------- LangGraph 原生 interrupt 审批流 ----------------
# 以下接口演示 LangGraph 原生 human-in-the-loop 中断点。
# 与上方基于 DB 状态机的审批流并存，供需要图内中断的场景使用。
# thread_store 保存 thread_id → 元信息，生产环境应替换为持久化存储。

thread_store: Dict[str, Dict[str, Any]] = {}
_MAX_THREADS = 1000


def _put_thread(thread_id: str, meta: Dict[str, Any]) -> None:
    """写入 thread_store，超限时按插入顺序删除最早的若干条目，防止无界增长。"""
    thread_store[thread_id] = meta
    if len(thread_store) > _MAX_THREADS:
        # dict 保留插入顺序，删除最早的多余条目
        overflow = len(thread_store) - _MAX_THREADS
        for key in list(thread_store.keys())[:overflow]:
            thread_store.pop(key, None)


def _get_or_create_interrupt_graph(app_state: AppState, tenant_id: str):
    """获取或创建带 interrupt 的图实例（按租户惰性创建，复用 checkpointer）

    P1-9 修复: 原实现复用 default 租户的 memory_store/company_kb 单例，导致非 default
    租户的 interrupt 评估读写到 default 租户的向量库，破坏多租户隔离。改为按 tenant_id
    取对应租户的 memory/kb store，并以 dict 缓存每租户的图实例（含独立 checkpointer，
    避免跨租户 thread_id 状态串扰）。
    """
    if not hasattr(app_state, "_interrupt_graphs"):
        app_state._interrupt_graphs = {}
    graph = app_state._interrupt_graphs.get(tenant_id)
    if graph is None:
        from agent.graph import create_evaluation_graph_with_interrupt
        from agent.tools import AgentToolkit

        toolkit = AgentToolkit(
            memory=app_state.get_memory_store(tenant_id),
            kb=app_state.get_kb_store(tenant_id),
        )
        graph = create_evaluation_graph_with_interrupt(
            toolkit=toolkit,
            model_router=app_state.model_router,
            prompt_loader=app_state.prompt_loader,
            multimodal_cleaner=app_state.multimodal_cleaner,
        )
        app_state._interrupt_graphs[tenant_id] = graph
    return graph


@router.post("/evaluations-interrupt", response_model=Dict[str, Any])
@rate_limit("10/minute")
async def create_evaluation_interrupt(
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(
        require_role(Role.EMPLOYEE, Role.MANAGER, Role.HR, Role.ADMIN)
    ),
):
    """
    启动带原生 interrupt 的评估工作流。
    图执行到审批节点时会暂停，返回 thread_id 与中断信息。
    调用方使用 /evaluations-interrupt/{thread_id}/resume 恢复执行。
    """
    employee_id = payload.get("employee_id")
    period = payload.get("period")
    raw_inputs = payload.get("raw_inputs", [])
    if not employee_id or not period:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="employee_id 和 period 必填",
        )

    # P1-10：复用 /evaluations 的输入护栏逻辑，raw_inputs 直接进图前必须先过护栏
    guard_result = _input_guard.check(
        [
            {
                "content": (
                    str(inp.get("content", "")) if isinstance(inp, dict) else str(inp)
                ),
                "attachments": (
                    inp.get("attachments", []) if isinstance(inp, dict) else []
                ),
            }
            for inp in raw_inputs
        ]
        or [{"content": "占位", "attachments": []}]
    )
    if not guard_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"输入被拦截: {guard_result.reason}",
        )

    tenant_id = get_current_tenant()
    graph = _get_or_create_interrupt_graph(app_state, tenant_id)
    thread_id = f"thread-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "employee_id": employee_id,
        "period": period,
        "raw_inputs": raw_inputs,
        "messages": [],
    }

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception:
        logger.exception("interrupt 评估图执行失败 employee_id=%s", employee_id)
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="create_evaluation_interrupt",
            employee_id=employee_id,
            details={
                "thread_id": thread_id,
                "period": period,
                "outcome": "exception",
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {
            "thread_id": thread_id,
            "status": "error",
            "error": "评估处理失败，请查看服务端日志",
        }

    # 检查是否在 interrupt 处暂停
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if interrupts:
        interrupt_info = (
            interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        )
        if isinstance(interrupt_info, str):
            interrupt_info = {"message": interrupt_info}
        _put_thread(
            thread_id,
            {
                "thread_id": thread_id,
                "employee_id": employee_id,
                "period": period,
                "status": "awaiting_review",
                "interrupt_node": interrupt_info.get("node", "unknown"),
                "interrupt_info": interrupt_info,
                "created_at": datetime.now(timezone.utc).isoformat(),
                # P0-IDOR: 记录创建者,后续 state/resume 校验归属
                "created_by": await get_current_user_id(request),
                # P1-9: 记录租户,后续 state/resume 取对应租户的图实例
                "tenant_id": tenant_id,
            },
        )
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="create_evaluation_interrupt",
            employee_id=employee_id,
            details={
                "thread_id": thread_id,
                "period": period,
                "outcome": "awaiting_review",
                "interrupt_node": interrupt_info.get("node", "unknown"),
            },
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {
            "thread_id": thread_id,
            "status": "awaiting_review",
            "interrupt": interrupt_info,
        }

    # 未触发 interrupt（错误或直接完成）
    if result.get("error"):
        await audit_service.log(
            actor_id=await get_current_user_id(request),
            action="create_evaluation_interrupt",
            employee_id=employee_id,
            details={"thread_id": thread_id, "period": period, "outcome": "error"},
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {
            "thread_id": thread_id,
            "status": "error",
            "error": "评估处理失败，请查看服务端日志",
        }
    parsed = result.get("parsed_evaluation")
    _put_thread(
        thread_id,
        {
            "thread_id": thread_id,
            "employee_id": employee_id,
            "period": period,
            "status": result.get("status", "completed"),
            "evaluation": parsed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
        },
    )
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="create_evaluation_interrupt",
        employee_id=employee_id,
        details={
            "thread_id": thread_id,
            "period": period,
            "outcome": result.get("status", "completed"),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return {
        "thread_id": thread_id,
        "status": result.get("status", "completed"),
        "evaluation": parsed,
    }


@router.get("/evaluations-interrupt/{thread_id}/state", response_model=Dict[str, Any])
async def get_interrupt_state(
    thread_id: str,
    request: Request,
    app_state: AppState = Depends(get_app_state),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """查询 interrupt 工作流当前状态"""
    meta = thread_store.get(thread_id)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在")
    # P0-IDOR: MANAGER 只能查看自己创建的线程,HR/ADMIN 可查看全部
    if role == Role.MANAGER and meta.get("created_by") != await get_current_user_id(
        request
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该线程"
        )

    graph = _get_or_create_interrupt_graph(
        app_state, meta.get("tenant_id", DEFAULT_TENANT_ID)
    )
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = await graph.aget_state(config)
    except Exception:
        logger.exception("查询 interrupt 状态失败 thread_id=%s", thread_id)
        state = None

    return {
        "thread_id": thread_id,
        "meta": meta,
        "next": list(state.next) if state else [],
        "values": state.values if state else {},
    }


@router.post("/evaluations-interrupt/{thread_id}/resume", response_model=Dict[str, Any])
async def resume_interrupt(
    thread_id: str,
    payload: Dict[str, Any],
    request: Request,
    app_state: AppState = Depends(get_app_state),
    eval_service: EvaluationService = Depends(get_evaluation_service),
    audit_service: AuditService = Depends(get_audit_service),
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.MANAGER, Role.HR, Role.ADMIN)),
):
    """
    恢复 interrupt 工作流，提交审批决策。
    payload: {"action": "approve"|"reject"|"request_hr_review", "comment": "..."}
    """
    from langgraph.types import Command

    meta = thread_store.get(thread_id)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在")
    if meta.get("status") not in ("awaiting_review", "hr_audit"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"线程状态 {meta.get('status')} 不可恢复",
        )
    # P0-IDOR: MANAGER 只能恢复自己创建的线程,HR/ADMIN 可恢复全部
    actor_id = await get_current_user_id(request)
    if role == Role.MANAGER and meta.get("created_by") != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该线程"
        )

    action = payload.get("action")
    if action not in ("approve", "reject", "request_hr_review"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action 必须为 approve / reject / request_hr_review",
        )

    resume_value = {
        "action": action,
        "comment": payload.get("comment", ""),
        "actor_id": actor_id,
    }

    # P1-10：resume 提交的 comment 接入输入护栏，拦截 Prompt 注入/恶意指令。
    # comment 为可选项，缺省时跳过护栏（与原 API 兼容）
    resume_comment = resume_value.get("comment") or ""
    if resume_comment.strip():
        guard_result = _input_guard.check([{"content": resume_comment}])
        if not guard_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"输入被拦截: {guard_result.reason}",
            )

    graph = _get_or_create_interrupt_graph(
        app_state, meta.get("tenant_id", DEFAULT_TENANT_ID)
    )
    config = {"configurable": {"thread_id": thread_id}}

    try:
        result = await graph.ainvoke(Command(resume=resume_value), config=config)
    except Exception:
        logger.exception("interrupt 工作流恢复失败 thread_id=%s", thread_id)
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="评估处理失败",
        )

    # 恢复后可能再次中断（如 manager_review → hr_audit）
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if interrupts:
        interrupt_info = (
            interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        )
        if isinstance(interrupt_info, str):
            interrupt_info = {"message": interrupt_info}
        meta["status"] = "awaiting_review"
        meta["interrupt_node"] = interrupt_info.get("node", "unknown")
        meta["interrupt_info"] = interrupt_info
        await audit_service.log(
            actor_id=actor_id,
            action=f"interrupt_{action}",
            employee_id=meta.get("employee_id"),
            details={"thread_id": thread_id, "next_node": interrupt_info.get("node")},
            ip_address=get_client_ip(request),
        )
        await session.commit()
        return {
            "thread_id": thread_id,
            "status": "awaiting_review",
            "interrupt": interrupt_info,
        }

    # 执行完成
    final_status = result.get("status", "completed")
    parsed = result.get("parsed_evaluation")

    # 持久化评估结果到数据库，成功后再更新内存中的线程状态，避免 DB 失败但内存已标记为完成
    if parsed and final_status in (
        EvaluationStatus.APPROVED,
        EvaluationStatus.REJECTED,
    ):
        try:
            await eval_service.create_evaluation(parsed)
            await audit_service.log(
                actor_id=actor_id,
                action=f"interrupt_{action}_finalized",
                evaluation_id=parsed.get("evaluation_id"),
                employee_id=meta.get("employee_id"),
                details={
                    "thread_id": thread_id,
                    "final_status": final_status,
                },
                ip_address=get_client_ip(request),
            )
            await session.commit()
            meta["status"] = final_status
            meta["evaluation"] = parsed
            # 进入终态后清理 thread_store，避免内存泄漏
            thread_store.pop(thread_id, None)
        except Exception:
            logger.exception("interrupt 评估结果持久化失败")
            await session.rollback()
            final_status = meta.get("status", "awaiting_review")
    else:
        # 非终态（理论上不应发生）也仅记录状态，不持久化评估
        meta["status"] = final_status
        meta["evaluation"] = parsed

    return {
        "thread_id": thread_id,
        "status": final_status,
        "evaluation": parsed,
    }


# ---------------- 租户管理 API ----------------


class CreateTenantRequest(BaseModel):
    """创建租户请求体"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    plan: str = Field(default="free", max_length=32)
    status: str = Field(default="active", max_length=16)


class UpdateTenantStatusRequest(BaseModel):
    """更新租户状态请求体"""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=16)


def _serialize_tenant(tenant: Tenant) -> Dict[str, Any]:
    return {
        "tenant_id": tenant.tenant_id,
        "name": tenant.name,
        "plan": tenant.plan,
        "status": tenant.status,
        "created_at": tenant.created_at.isoformat(),
    }


@router.post("/tenants", response_model=Dict[str, Any])
async def create_tenant(
    payload: CreateTenantRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """创建租户（仅 ADMIN）。租户是全局主体，不受 current_tenant 过滤。"""
    existing = await session.execute(
        select(Tenant).where(Tenant.tenant_id == payload.tenant_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"租户 tenant_id={payload.tenant_id} 已存在",
        )
    tenant = Tenant(
        tenant_id=payload.tenant_id,
        name=payload.name,
        plan=payload.plan,
        status=payload.status,
    )
    session.add(tenant)
    await session.flush()
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="create_tenant",
        details={
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "plan": tenant.plan,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return _serialize_tenant(tenant)


@router.get("/tenants", response_model=Dict[str, Any])
async def list_tenants(
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """列出全部租户（仅 ADMIN）"""
    result = await session.execute(select(Tenant).order_by(Tenant.created_at))
    tenants = result.scalars().all()
    return {"items": [_serialize_tenant(t) for t in tenants], "count": len(tenants)}


@router.get("/tenants/{tenant_id}", response_model=Dict[str, Any])
async def get_tenant(
    tenant_id: str,
    session: AsyncSession = Depends(get_db),
    role: Role = Depends(require_role(Role.HR, Role.ADMIN)),
):
    """查询单个租户详情（HR/ADMIN）。HR 仅能查看自己所属租户，ADMIN 可查任意租户。"""
    if role == Role.HR and tenant_id != get_current_tenant():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HR 仅能查看自己所属租户",
        )
    result = await session.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")
    return _serialize_tenant(tenant)


@router.put("/tenants/{tenant_id}/status", response_model=Dict[str, Any])
async def update_tenant_status(
    tenant_id: str,
    payload: UpdateTenantStatusRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    audit_service: AuditService = Depends(get_audit_service),
    role: Role = Depends(require_role(Role.ADMIN)),
):
    """更新租户状态（仅 ADMIN），如 active/suspended/disabled"""
    result = await session.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")
    old_status = tenant.status
    tenant.status = payload.status
    await session.flush()
    await audit_service.log(
        actor_id=await get_current_user_id(request),
        action="update_tenant_status",
        details={
            "tenant_id": tenant_id,
            "old_status": old_status,
            "new_status": payload.status,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    # 状态变更后立即失效租户缓存，避免 middleware 仍按旧状态放行/拦截
    from api.middleware import invalidate_tenant_cache

    invalidate_tenant_cache(tenant_id)
    return _serialize_tenant(tenant)
