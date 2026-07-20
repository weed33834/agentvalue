"""
语音 TTS / STT API Router

端点:
- POST /api/v1/voice/tts  文本转语音(Text-To-Speech)
- POST /api/v1/voice/stt  语音转文本(Speech-To-Text)

实现策略:
- TTS: 优先调用 OpenAI Audio Speech API (tts-1), 返回 mp3 base64;
  未配置 openai_api_key 时降级为 JSON 提示前端使用浏览器 Web Speech API。
- STT: 优先调用 OpenAI Audio Transcriptions API (whisper-1), 接收 wav/webm 上传;
  未配置 key 或请求失败时返回错误提示。

参考:
- OpenAI TTS:   POST https://api.openai.com/v1/audio/speech
- OpenAI STT:   POST https://api.openai.com/v1/audio/transcriptions
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

# OpenAI Audio API 端点(支持通过 openai_base_url 覆盖, 兼容代理/兼容层)
_OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
_OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"

# TTS 默认参数
_DEFAULT_TTS_MODEL = "tts-1"
_DEFAULT_TTS_VOICE = "alloy"
_DEFAULT_TTS_FORMAT = "mp3"
_DEFAULT_TTS_SPEED = 1.0

# STT 默认参数
_DEFAULT_STT_MODEL = "whisper-1"

# 文本长度上限(OpenAI TTS 单次上限约 4096 字符)
_MAX_TTS_TEXT_LENGTH = 4096
# 上传音频大小上限(25MB, OpenAI Whisper 限制)
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
# 允许的音频 content-type
_ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/webm",
    "audio/webm;codecs=opus",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/m4a",
    "application/octet-stream",  # 部分浏览器上传无明确 content-type
}


# ---------------- Schemas ----------------


class TTSRequest(BaseModel):
    """TTS 请求体"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_MAX_TTS_TEXT_LENGTH, description="要合成的文本")
    voice: Optional[str] = Field(
        default=None,
        description="音色: alloy/echo/fable/onyx/nova/shimmer",
    )
    speed: Optional[float] = Field(
        default=None, ge=0.25, le=4.0, description="语速 0.25-4.0"
    )


class TTSResponse(BaseModel):
    """TTS 响应体"""

    model_config = ConfigDict(extra="forbid")

    success: bool
    audio: Optional[str] = Field(
        default=None, description="base64 编码的音频数据"
    )
    format: Optional[str] = Field(default=None, description="音频格式: mp3")
    fallback: Optional[bool] = Field(
        default=None, description="是否降级到浏览器 Web Speech API"
    )
    message: Optional[str] = Field(default=None, description="提示信息")
    voice: Optional[str] = None
    speed: Optional[float] = None


class STTResponse(BaseModel):
    """STT 响应体"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="识别出的文本")


# ---------------- TTS Endpoint ----------------


def _get_openai_settings():
    """获取 OpenAI 配置(key + base_url), 返回 (key, base_url) 或 (None, None)"""
    try:
        settings = get_settings()
        key = getattr(settings, "openai_api_key", None)
        base_url = getattr(settings, "openai_base_url", None) or "https://api.openai.com/v1"
        return key, base_url
    except Exception as e:
        logger.warning("获取 OpenAI 配置失败: %s", e)
        return None, None


@router.post("/tts", response_model=TTSResponse)
async def text_to_speech(payload: TTSRequest):
    """文本转语音(TTS)

    优先使用 OpenAI TTS API, 返回 mp3 base64;
    未配置 openai_api_key 时降级, 提示前端使用浏览器 Web Speech API。
    """
    text = payload.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="文本不能为空"
        )

    voice = payload.voice or _DEFAULT_TTS_VOICE
    speed = payload.speed if payload.speed is not None else _DEFAULT_TTS_SPEED

    api_key, base_url = _get_openai_settings()
    if not api_key:
        # 降级: 前端使用浏览器 Web Speech API (SpeechSynthesis)
        logger.info("未配置 openai_api_key, TTS 降级到浏览器 Web Speech API")
        return TTSResponse(
            success=False,
            fallback=True,
            message=(
                "服务端未配置 TTS 引擎, 请使用浏览器 Web Speech API "
                "(window.speechSynthesis) 进行语音合成。"
            ),
            voice=voice,
            speed=speed,
        )

    # 调用 OpenAI Audio Speech API
    tts_url = f"{base_url.rstrip('/')}/audio/speech" if base_url else _OPENAI_TTS_URL
    request_body = {
        "model": _DEFAULT_TTS_MODEL,
        "input": text,
        "voice": voice,
        "response_format": _DEFAULT_TTS_FORMAT,
        "speed": speed,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(tts_url, json=request_body, headers=headers)

        if resp.status_code != 200:
            err_text = resp.text[:500] if resp.text else ""
            logger.warning(
                "OpenAI TTS 返回 %s: %s", resp.status_code, err_text
            )
            # API 调用失败也降级到浏览器
            return TTSResponse(
                success=False,
                fallback=True,
                message=(
                    f"TTS 服务暂不可用 (HTTP {resp.status_code}), "
                    "请使用浏览器 Web Speech API。"
                ),
                voice=voice,
                speed=speed,
            )

        # 响应为二进制音频, base64 编码返回
        audio_b64 = base64.b64encode(resp.content).decode("ascii")
        return TTSResponse(
            success=True,
            audio=audio_b64,
            format=_DEFAULT_TTS_FORMAT,
            fallback=False,
            message="ok",
            voice=voice,
            speed=speed,
        )
    except ImportError:
        logger.warning("httpx 未安装, TTS 降级到浏览器 Web Speech API")
        return TTSResponse(
            success=False,
            fallback=True,
            message="服务端未安装 httpx, 请使用浏览器 Web Speech API。",
            voice=voice,
            speed=speed,
        )
    except Exception as e:
        logger.warning("TTS 调用异常: %s", e)
        return TTSResponse(
            success=False,
            fallback=True,
            message=f"TTS 服务异常: {e}, 请使用浏览器 Web Speech API。",
            voice=voice,
            speed=speed,
        )


# ---------------- STT Endpoint ----------------


@router.post("/stt", response_model=STTResponse)
async def speech_to_text(file: UploadFile = File(..., description="音频文件 wav/webm")):
    """语音转文本(STT)

    接收 audio/wav 或 audio/webm 文件上传, 优先调用 OpenAI Whisper API。
    未配置 key 或识别失败时返回错误提示。
    """
    # 校验文件类型(宽松: 部分浏览器 content-type 不准确, 用扩展名兜底)
    content_type = (file.content_type or "").lower()
    filename = (file.filename or "").lower()
    is_audio = content_type in _ALLOWED_AUDIO_TYPES
    if not is_audio:
        # 扩展名兜底
        audio_exts = (".wav", ".webm", ".mp3", ".m4a", ".ogg", ".mp4")
        if any(filename.endswith(ext) for ext in audio_exts):
            is_audio = True
    if not is_audio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持 audio/wav, audio/webm 等音频格式",
        )

    # 读取音频内容
    try:
        audio_bytes = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"读取音频文件失败: {e}",
        )
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="音频文件为空"
        )
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"音频文件过大(上限 {_MAX_AUDIO_BYTES // 1024 // 1024}MB)",
        )

    api_key, base_url = _get_openai_settings()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务端未配置 OpenAI API key, STT 不可用。请配置 openai_api_key。",
        )

    # 调用 OpenAI Audio Transcriptions API (Whisper)
    stt_url = (
        f"{base_url.rstrip('/')}/audio/transcriptions"
        if base_url
        else _OPENAI_STT_URL
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    # 确定上传文件名(Whisper 需要扩展名来识别格式)
    ext = ".webm"
    if "wav" in content_type or filename.endswith(".wav"):
        ext = ".wav"
    elif filename.endswith(".mp3"):
        ext = ".mp3"
    elif filename.endswith(".m4a"):
        ext = ".m4a"
    elif filename.endswith(".ogg"):
        ext = ".ogg"
    upload_filename = filename or f"audio{ext}"
    if not upload_filename.endswith(
        (".wav", ".webm", ".mp3", ".m4a", ".ogg", ".mp4")
    ):
        upload_filename = f"audio{ext}"

    try:
        import httpx

        files = {"file": (upload_filename, audio_bytes, content_type or "audio/webm")}
        data = {"model": _DEFAULT_STT_MODEL}

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(stt_url, headers=headers, files=files, data=data)

        if resp.status_code != 200:
            err_text = resp.text[:500] if resp.text else ""
            logger.warning("OpenAI STT 返回 %s: %s", resp.status_code, err_text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"语音识别服务返回错误 (HTTP {resp.status_code}): {err_text}",
            )

        result = resp.json()
        recognized_text = (result.get("text") or "").strip()
        return STTResponse(text=recognized_text)
    except HTTPException:
        raise
    except ImportError:
        logger.warning("httpx 未安装, STT 不可用")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务端未安装 httpx, STT 不可用。",
        )
    except Exception as e:
        logger.warning("STT 调用异常: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"语音识别服务异常: {e}",
        )
