"""
Prompt Playground SSE API

对标 Langfuse Playground (https://langfuse.com/docs/prompts/get-started)

路由前缀: /api/v1/admin/playground
权限: Role.ADMIN

核心端点:
- POST /run: 流式运行 Prompt,SSE 响应

SSE 事件协议:
- trace:  Langfuse trace URL
- token:  LLM token 增量(content 字段)
- tool_call_start:  工具调用决策完成(name/id/index)
- tool_call_delta:  arguments 增量(跨 chunk 累加)
- tool_call_end:    arguments 收完(完整 args)
- done:   完成(output/usage/latency_ms)
- error:  错误(message)
- ping:   心跳(空 data)

设计要点(参考 sse-starlette 最佳实践):
- asyncio.Queue(maxsize=16) 背压缓冲
- 每帧前 request.is_disconnected() 检查客户端断连
- asyncio.CancelledError 必须 reraise (防任务泄漏)
- ping=15s 心跳防代理超时
- send_timeout=5s 写超时保护
- X-Accel-Buffering: no 禁 nginx 缓冲
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db_prompt_loader import get_global_db_prompt_loader
from auth.rbac import Role, require_role
from core.database import get_db
from core.providers.base import ChatMessage, StreamChunk
from core.providers.stream_buffer import ToolCallAggregator
from models.models import PromptLabel, PromptTemplate, PromptVersion

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/playground",
    tags=["admin-playground"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

try:
    from sse_starlette.sse import EventSourceResponse
    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False
    EventSourceResponse = None  # type: ignore


# ============================================================
# Request Schema
# ============================================================


class PlaygroundRunRequest(BaseModel):
    prompt_name: str = Field(..., description="Prompt 模板名")
    # 前端传 prompt_version,后端兼容 prompt_version 与 version 两种字段名
    prompt_version: Optional[int] = Field(None, alias="prompt_version", description="版本号")
    label: Optional[str] = Field("production", description="Label(二选一)")
    variables: Dict[str, Any] = Field(default_factory=dict, description="模板变量")
    model_overrides: Optional[Dict[str, Any]] = Field(
        None, description="覆盖 prompt.config 中的 model/temperature 等"
    )
    # 前端直传 model_name 时也接受(覆盖 model_overrides.model)
    model_name: Optional[str] = Field(None, description="指定模型名,覆盖 prompt config")
    temperature: Optional[float] = Field(None, description="覆盖 temperature")
    max_tokens: Optional[int] = Field(None, description="覆盖 max_tokens")
    tools: Optional[List[str]] = Field(
        None, description="启用的工具列表(用于 ReAct 调试)"
    )
    thread_id: Optional[str] = Field(None, description="多轮对话用")

    model_config = {"populate_by_name": True}


# ============================================================
# SSE 事件格式
# ============================================================


def _sse_event(event: str, data: Dict[str, Any]) -> Dict[str, str]:
    """构造 SSE 事件 dict"""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


# ============================================================
# 核心端点: POST /run
# ============================================================


@router.post("/run")
async def playground_run(
    req: PlaygroundRunRequest,
    http_request: Request,
):
    """流式运行 Prompt,SSE 响应。

    对标 Langfuse Playground:选 prompt 版本 + 填变量 + 点 Run → 流式看 LLM 输出。
    """
    if not _SSE_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sse-starlette 未安装,请 pip install sse-starlette",
        )

    return EventSourceResponse(
        _run_stream(req, http_request),
        ping=15,
        send_timeout=5.0,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


async def _run_stream(req: PlaygroundRunRequest, http_request: Request):
    """SSE 生成器: 背压队列 + 断连检测 + CancelledError reraise。

    架构(参考 sse-starlette 最佳实践):
    1. 创建 bounded asyncio.Queue(maxsize=16) 做背压
    2. 启动 producer task 执行 LLM 调用,token 推入 queue
    3. 主循环从 queue 拉数据 yield 给 SSE
    4. 每帧前检查 request.is_disconnected()
    5. 25s 无数据时发 ping 心跳
    6. CancelledError 必须 reraise (防任务泄漏)
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)

    async def producer():
        try:
            await _execute_playground(req, queue)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Playground producer 异常: %s", e, exc_info=True)
            await queue.put(_sse_event("error", {"message": str(e)}))
        finally:
            await queue.put(None)  # 哨兵

    task = asyncio.create_task(producer())
    try:
        while True:
            # 关键: 每帧前检查客户端是否断连
            if await http_request.is_disconnected():
                task.cancel()
                break
            try:
                # 25s 无数据时发 ping 心跳(防代理超时)
                item = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield _sse_event("ping", {})
                continue
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        # 客户端断连后 Uvicorn 会取消任务,必须 reraise
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ============================================================
# 核心执行逻辑
# ============================================================


async def _execute_playground(req: PlaygroundRunRequest, queue: asyncio.Queue):
    """执行 Playground 请求,将事件推入 queue。"""
    start_time = time.time()

    # 1. 解析 Prompt 版本
    version_info = await _resolve_prompt_version(req)
    if version_info is None:
        await queue.put(
            _sse_event(
                "error",
                {"message": f"Prompt '{req.prompt_name}' 未找到版本 {req.prompt_version or req.label}"},
            )
        )
        return

    version, template = version_info

    # 2. 渲染 Prompt
    rendered = _render_prompt(version, req.variables)

    # 3. 合并 config overrides (优先级: 顶层字段 > model_overrides > version.config > 默认)
    config = {**(version.config or {}), **(req.model_overrides or {})}
    model_name = req.model_name or config.get("model", "gpt-4o-mini")
    temperature = req.temperature if req.temperature is not None else config.get("temperature", 0.3)
    max_tokens = req.max_tokens or config.get("max_tokens", 4096)

    # 4. 发 trace 事件 (Langfuse trace URL)
    trace_id = str(uuid.uuid4())
    trace_url = f"https://cloud.langfuse.com/trace/{trace_id}"
    await queue.put(
        _sse_event(
            "trace",
            {
                "trace_id": trace_id,
                "trace_url": trace_url,
                "prompt_version": version.version,
                "prompt_version_id": version.id,
                "model": model_name,
            },
        )
    )

    # 5. 获取 LLM Provider (按 model_name 路由到对应 Provider 类)
    provider = await _get_provider_for_playground(model_name)

    if provider is None:
        await queue.put(
            _sse_event("error", {"message": f"无法获取模型 {model_name} 的 Provider,请先在 Provider 管理页配置对应凭证"})
        )
        return

    # 6. 流式调用 LLM
    messages = [ChatMessage(role="user", content=rendered)]
    tools_schema = await _build_tools_schema(req.tools) if req.tools else None
    aggregator = ToolCallAggregator()
    full_content = ""
    last_usage: Dict[str, Any] = {}

    try:
        async for chunk in provider.stream_chat_completion(
            messages=messages,
            tools=tools_schema,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            # 文本 token
            if chunk.content:
                full_content += chunk.content
                await queue.put(
                    _sse_event("token", {"content": chunk.content})
                )

            # tool_calls delta
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    # 首个 chunk 携带 name+id → 发 tool_call_start
                    if tc.name and tc.id:
                        await queue.put(
                            _sse_event(
                                "tool_call_start",
                                {
                                    "name": tc.name,
                                    "id": tc.id,
                                    "index": tc.index,
                                },
                            )
                        )
                    # 每个 chunk 都发 delta
                    if tc.arguments:
                        await queue.put(
                            _sse_event(
                                "tool_call_delta",
                                {"index": tc.index, "args": tc.arguments},
                            )
                        )
                aggregator.feed(chunk)

            # usage (最后一个 chunk)
            if chunk.usage:
                last_usage = chunk.usage

        # stream 结束后无条件 finalize(不依赖 finish_reason,Anthropic/Gemini 可能不返回 tool_calls)
        final_calls = aggregator.finalize()
        for call in final_calls:
            await queue.put(
                _sse_event(
                    "tool_call_end",
                    {
                        "index": final_calls.index(call),
                        "id": call.get("id", ""),
                        "name": call.get("name", ""),
                        "args": json.dumps(call.get("arguments", {}), ensure_ascii=False, default=str),
                    },
                )
            )

    except Exception as e:
        await queue.put(_sse_event("error", {"message": f"LLM 调用失败: {e}"}))
        return

    # 7. 发 done 事件
    latency_ms = int((time.time() - start_time) * 1000)
    await queue.put(
        _sse_event(
            "done",
            {
                "output": full_content,
                "usage": last_usage,
                "latency_ms": latency_ms,
                "model": model_name,
                "prompt_version": version.version,
                "finish_reason": "stop",
            },
        )
    )


# ============================================================
# Helpers
# ============================================================


async def _resolve_prompt_version(req: PlaygroundRunRequest):
    """解析 Prompt 版本(优先 prompt_version,其次 label)"""
    try:
        loader = get_global_db_prompt_loader()
        if req.prompt_version is not None:
            version = await loader.get_version(req.prompt_name, req.prompt_version)
        else:
            version = await loader.get_for_request(
                name=req.prompt_name,
                employee_id=req.thread_id or "playground",
            )
        if version is None:
            return None
        # 拉 template
        from core.database import get_db_session

        async with get_db_session() as sess:
            stmt = select(PromptTemplate).where(
                PromptTemplate.id == version.template_id
            )
            result = await sess.execute(stmt)
            template = result.scalar_one_or_none()
        return version, template
    except Exception as e:
        logger.warning("_resolve_prompt_version 失败: %s", e)
        return None


def _render_prompt(version, variables: Dict[str, Any]) -> str:
    """渲染 Prompt (简单变量替换)"""
    content = version.content
    for k, v in variables.items():
        content = content.replace("{{ " + k + " }}", str(v))
        content = content.replace("{{" + k + "}}", str(v))
    return content


async def _get_provider_for_playground(model_name: str):
    """按 model_name 路由到对应 Provider 实例。

    优先级:
    1. 从 tenant_provider_models 查找 model_name 对应 provider + 活跃凭证(若已配置)
    2. 按 model_name 前缀(gpt/claude/gemini/llama)选择 Provider 类,
       凭证从 settings.cloud_api_key 兜底(便于未配置也能跑 OpenAI)
    3. 失败则返回 None
    """
    from core.config import get_settings
    from core.providers import (
        AnthropicProvider,
        GeminiProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
    )
    from core.providers.base import ProviderConfig

    settings = get_settings()

    # 1. 先查 DB:tenant 是否为该 model 配置过凭证
    try:
        provider_name, credentials = await _lookup_tenant_credential_for_model(model_name)
    except Exception as e:
        logger.warning("_lookup_tenant_credential_for_model 失败: %s", e)
        provider_name, credentials = None, None

    # 2. 若 DB 未命中,按 model_name 前缀推断 provider + 从 settings 兜底
    if provider_name is None:
        provider_name, credentials = _infer_provider_from_model_name(model_name, settings)

    if provider_name is None:
        logger.warning("无法识别 model_name=%s 的 provider 类型", model_name)
        return None

    # 3. 构造 ProviderConfig
    api_key = (credentials or {}).get("api_key") if credentials else None
    api_base = (credentials or {}).get("api_base")

    config = ProviderConfig(
        model_name=model_name,
        api_key=api_key,
        base_url=api_base,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        model_tier="playground",
        request_timeout=settings.llm_request_timeout,
    )

    if provider_name == "anthropic":
        return AnthropicProvider(config)
    if provider_name == "gemini":
        return GeminiProvider(config)
    if provider_name == "ollama":
        return OllamaProvider(config)
    # 默认走 OpenAI 兼容
    return OpenAICompatibleProvider(config)


async def _lookup_tenant_credential_for_model(model_name: str):
    """从 tenant_provider_models + tenant_provider_credentials 查活跃凭证。

    返回 (provider_name, credentials_dict) 或 (None, None)。
    """
    from core.database import get_db_session
    from core.tenant_context import get_current_tenant
    from core.providers.credential_service import ProviderCredentialService
    from models.provider_models import (
        TenantProvider,
        TenantProviderCredential,
        TenantProviderModel,
    )

    tenant_id = get_current_tenant()
    async with get_db_session() as sess:
        # 找 tenant 中匹配 model_name 的绑定
        mstmt = select(TenantProviderModel).where(
            TenantProviderModel.tenant_id == tenant_id,
            TenantProviderModel.model_name == model_name,
            TenantProviderModel.enabled.is_(True),
        )
        mresult = await sess.execute(mstmt)
        tm = mresult.scalar_one_or_none()
        provider_name = None
        active_cred_id = None
        if tm:
            provider_name = tm.provider
            active_cred_id = tm.active_credential_id
        else:
            # 退而求其次:tenant_provider_credentials 找匹配的(用 active)
            cstmt = select(TenantProvider, TenantProviderCredential).where(
                TenantProvider.tenant_id == tenant_id,
                TenantProvider.enabled.is_(True),
                TenantProviderCredential.tenant_id == tenant_id,
            )
            cresult = await sess.execute(cstmt)
            for tp, cred in cresult.all():
                if tp.active_credential_id == cred.id:
                    provider_name = tp.provider
                    active_cred_id = cred.id
                    break
        if not provider_name or not active_cred_id:
            return None, None
        # 拿凭证
        cred_stmt = select(TenantProviderCredential).where(
            TenantProviderCredential.id == active_cred_id
        )
        cred_result = await sess.execute(cred_stmt)
        cred_row = cred_result.scalar_one_or_none()
        if not cred_row:
            return provider_name, None
        svc = ProviderCredentialService(sess)
        plain = svc.decrypt_credential(cred_row.encrypted_config)
        return provider_name, plain


def _infer_provider_from_model_name(model_name: str, settings):
    """按 model_name 前缀推断 provider,凭证从 settings 兜底。"""
    name = (model_name or "").lower()
    if name.startswith("claude") or "anthropic" in name:
        return "anthropic", {"api_key": getattr(settings, "anthropic_api_key", None) or ""}
    if name.startswith("gemini") or "gemini" in name:
        return "gemini", {"api_key": getattr(settings, "gemini_api_key", None) or ""}
    if name.startswith("llama") or name.startswith("qwen") or "ollama" in name or ":" in name:
        # Ollama 模型通常带 :tag(如 llama3.1:8b)
        return "ollama", {"api_base": getattr(settings, "local_base_url", None) or "http://localhost:11434"}
    # 默认 OpenAI 兼容
    api_key = settings.cloud_api_key or settings.openai_api_key
    api_base = settings.cloud_base_url or settings.openai_base_url
    return "openai", {"api_key": api_key, "api_base": api_base}


async def _build_tools_schema(tool_names: List[str]):
    """构建工具 schema(用于 LLM function calling)"""
    try:
        from agent.langchain_tools import build_langchain_tools, list_available_tools

        # 返回 OpenAI function schema 格式
        # 简化: 直接用 list_available_tools 获取元数据
        tools_meta = list_available_tools(",".join(tool_names))
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("args_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools_meta
            if t["name"] in tool_names
        ]
    except Exception as e:
        logger.warning("_build_tools_schema 失败: %s", e)
        return None
