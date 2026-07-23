"""
ModelRouter：硬件探测 + 档位选择 + Provider 路由 + 自动降级
"""

import logging
import subprocess
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple

from .circuit_breaker import (
    CircuitBreakerRegistry,
    call_with_circuit,
    get_global_registry,
)
from .config import Settings, get_settings
from .metrics import set_provider_health_score
from .providers.base import BaseProvider, ProviderConfig
from .providers.health_cache import HealthCheckCache, get_global_health_cache
from .providers.openai_provider import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

ModelTier = Literal["L0", "L1", "L2", "L3"]

# 网络延迟探测的固定上限（秒），避免无外网环境下长时间阻塞
_NETWORK_PROBE_TIMEOUT = 2.0
# 健康度评分滑动窗口：保留最近 N 次 health_check 记录
_HEALTH_WINDOW = 10


@dataclass
class TierInfo:
    """档位信息"""

    tier: ModelTier
    model_name: str
    provider_type: str
    description: str
    min_vram_gb: Optional[float] = None
    min_ram_gb: Optional[float] = None


class ModelRouter:
    """
    模型路由器
    - 根据硬件和配置选择最合适的模型档位
    - 提供 Provider 实例
    - 支持健康检查和自动降级
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._hardware = self._detect_hardware(self.settings.cloud_base_url)
        self._tier_map = self._build_tier_map()
        # 各档位最近 N 次 health_check 记录：(success, response_time_s)
        self._health_history: Dict[str, Deque[Tuple[bool, float]]] = {
            tier: deque(maxlen=_HEALTH_WINDOW) for tier in ("L0", "L1", "L2", "L3")
        }
        # P1 增强: 状态机熔断器(参考 LiteLLM) + 健康检查缓存(避免重复打 /models)
        self._circuit_registry: CircuitBreakerRegistry = get_global_registry()
        self._health_cache: HealthCheckCache = get_global_health_cache()

    def _build_tier_map(self) -> Dict[ModelTier, TierInfo]:
        return {
            "L0": TierInfo(
                tier="L0",
                model_name=self.settings.cloud_model or self.settings.openai_model,
                provider_type="cloud",
                description="云端大模型，最强推理能力",
            ),
            "L1": TierInfo(
                tier="L1",
                model_name=self.settings.local_model_l1,
                provider_type="local",
                description="本地边缘小模型，纯文本摘要",
                min_vram_gb=0,
                min_ram_gb=4,
            ),
            "L2": TierInfo(
                tier="L2",
                model_name=self.settings.local_model_l2,
                provider_type="local",
                description="本地标准模型，文本+表格分析",
                min_vram_gb=6,
                min_ram_gb=12,
            ),
            "L3": TierInfo(
                tier="L3",
                model_name=self.settings.local_model_l3,
                provider_type="local",
                description="本地旗舰模型，全模态深度推理",
                min_vram_gb=12,
                min_ram_gb=24,
            ),
        }

    @staticmethod
    def _detect_hardware(cloud_base_url: Optional[str] = None) -> Dict[str, Any]:
        """探测硬件资源

        在原有 VRAM/RAM/GPU 基础上，额外采集 CPU 核数、磁盘可用空间、网络延迟，
        供 auto 档更精细地选择本地档位与判断云端可达性。
        cloud_base_url 为 None 时（如静态调用）跳过网络延迟探测。
        """
        result: Dict[str, Any] = {
            "vram_gb": 0.0,
            "ram_gb": 0.0,
            "gpu_count": 0,
            "gpu_names": [],
            "cpu_count": 0,
            "disk_free_gb": 0.0,
            "network_latency_s": None,
        }

        # 内存 / CPU / 磁盘（统一通过 psutil，单项失败不影响其他探测）
        try:
            import psutil

            try:
                result["ram_gb"] = psutil.virtual_memory().total / (1024**3)
            except Exception as e:
                logger.warning(f"无法检测内存: {e}")
            try:
                # physical cores（非逻辑线程），更贴合本地模型可用的真实算力
                cpu = psutil.cpu_count(logical=False)
                result["cpu_count"] = int(cpu) if cpu else 0
            except Exception as e:
                logger.warning(f"无法检测 CPU 核数: {e}")
            try:
                result["disk_free_gb"] = psutil.disk_usage("/").free / (1024**3)
            except Exception as e:
                logger.warning(f"无法检测磁盘可用空间: {e}")
        except Exception as e:
            logger.warning(f"psutil 不可用，跳过 CPU/内存/磁盘探测: {e}")

        # GPU / 显存
        try:
            import torch

            if torch.cuda.is_available():
                result["gpu_count"] = torch.cuda.device_count()
                for i in range(result["gpu_count"]):
                    props = torch.cuda.get_device_properties(i)
                    result["vram_gb"] += props.total_memory / (1024**3)
                    result["gpu_names"].append(props.name)
        except Exception as e:
            logger.debug(f"torch 不可用，尝试 nvidia-smi: {e}")
            #  fallback：尝试 nvidia-smi
            try:
                output = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.total",
                        "--format=csv,noheader",
                    ],
                    text=True,
                )
                total_vram_mb = 0
                for line in output.strip().splitlines():
                    parts = line.split(",")
                    if len(parts) >= 2:
                        result["gpu_names"].append(parts[0].strip())
                        mem_str = (
                            parts[1].strip().replace(" MiB", "").replace(" MB", "")
                        )
                        total_vram_mb += int(mem_str)
                result["vram_gb"] = total_vram_mb / 1024
                result["gpu_count"] = len(result["gpu_names"])
            except Exception as e2:
                logger.warning(f"无法检测 GPU: {e2}")

        # 网络延迟：探测 L0 health_check 端点（{base_url}/models），timeout 2s
        if cloud_base_url:
            url = cloud_base_url.rstrip("/")
            if not url.endswith("/models"):
                url = url + "/models"
            try:
                start = time.monotonic()
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(
                    req, timeout=_NETWORK_PROBE_TIMEOUT
                ) as _resp:
                    # 仅测可达性与延迟，不消费响应体；读到状态码即可
                    _ = _resp.status
                result["network_latency_s"] = time.monotonic() - start
            except Exception as e:
                logger.debug(
                    f"网络延迟探测失败（cloud_base_url={cloud_base_url}）: {e}"
                )
                result["network_latency_s"] = None

        return result

    def get_recommended_tier(self) -> ModelTier:
        """根据硬件推荐默认档位"""
        if self.settings.model_tier != "auto":
            return self.settings.model_tier  # type: ignore

        # 优先尝试本地高档位
        vram = self._hardware.get("vram_gb", 0)
        ram = self._hardware.get("ram_gb", 0)

        if (
            vram >= self._tier_map["L3"].min_vram_gb
            and ram >= self._tier_map["L3"].min_ram_gb
        ):
            return "L3"
        if (
            vram >= self._tier_map["L2"].min_vram_gb
            and ram >= self._tier_map["L2"].min_ram_gb
        ):
            return "L2"
        if ram >= self._tier_map["L1"].min_ram_gb:
            return "L1"

        # 无本地条件则回退云端
        return "L0"

    def get_provider(self, tier: Optional[ModelTier] = None) -> BaseProvider:
        """根据档位返回 Provider 实例"""
        selected_tier = tier or self.get_recommended_tier()
        tier_info = self._tier_map[selected_tier]

        # 仅 model_name/base_url/api_key 随档位类型变化,其余字段对齐 settings
        if tier_info.provider_type == "cloud":
            api_key = self.settings.cloud_api_key or self.settings.openai_api_key
            base_url = self.settings.cloud_base_url or self.settings.openai_base_url
            # 实时读 model_name 支持 admin LLM 配置 API 运行时修改
            model_name = self.settings.cloud_model or self.settings.openai_model
            if not api_key:
                logger.warning(
                    f"档位 {selected_tier} 为云端模型，但 cloud_api_key/openai_api_key 未配置，"
                    "调用将失败或回退到本地模型"
                )
        else:
            local_model_map = {
                "L1": self.settings.local_model_l1,
                "L2": self.settings.local_model_l2,
                "L3": self.settings.local_model_l3,
            }
            model_name = local_model_map.get(selected_tier, tier_info.model_name)
            base_url = self.settings.local_base_url
            api_key = self.settings.local_api_key

        config = ProviderConfig(
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
            model_tier=selected_tier,
            embedding_model=self.settings.embedding_model,
            vision_model=self.settings.vision_model,
            request_timeout=self.settings.llm_request_timeout,
        )
        return OpenAICompatibleProvider(config)

    async def get_provider_with_fallback(self) -> tuple[BaseProvider, ModelTier]:
        """
        获取可用 Provider，如果首选不可用则自动降级
        返回 (provider, tier)

        在保留原有降级链路的基础上，对每次 health_check 计时并记录健康度评分，
        供 runtime_reselect 与 Prometheus 指标消费。

        P1 增强:
        - 熔断器: CLOSED→OPEN→HALF-OPEN,5 次连续失败熔断 fast-fail 0ms,60s 探活
        - 健康检查缓存: 30s 内重复调用直接返回缓存,避免浪费上游 /models 配额
        """
        preferred_tier = self.get_recommended_tier()
        order: List[ModelTier] = [preferred_tier]

        # 构造降级顺序
        fallback_order: List[ModelTier] = ["L0", "L3", "L2", "L1"]
        for t in fallback_order:
            if t not in order:
                order.append(t)

        last_error = None
        for tier in order:
            # P1: 检查熔断器状态,OPEN 直接跳过(0ms,不发网络请求)
            circuit_key = f"{tier}"
            circuit = self._circuit_registry.get_or_create(circuit_key)
            if circuit.is_open():
                logger.info("档位 %s 熔断器 OPEN,跳过降级到下一档", tier)
                continue

            provider = self.get_provider(tier)

            # P1: 用缓存避免重复打 /models(30s TTL,失败 10s)
            cache_key = f"{tier}:{provider.name()}"

            async def _do_health_check():
                return await provider.health_check()

            healthy, elapsed = await self._health_cache.get_or_refresh(
                cache_key, _do_health_check
            )
            self.record_health_check(tier, success=bool(healthy), response_time=elapsed)

            if healthy:
                # 二次检查熔断器(可能在 health_check 期间被其他请求触发熔断)
                if circuit.is_open():
                    logger.info("档位 %s 健康检查通过但熔断器已 OPEN,跳过", tier)
                    continue
                logger.info(f"ModelRouter 选择档位: {tier}")
                return provider, tier
            else:
                # 健康检查失败: 触发熔断器 record_failure
                await circuit.record_failure()
                last_error = RuntimeError(f"档位 {tier} 健康检查失败")
                logger.warning(f"档位 {tier} 健康检查失败,熔断器失败计数 +1")

        # 如果全部失败，返回首选让调用方在运行时重试/报错
        logger.error(
            f"所有档位均不可用，返回首选档位: {preferred_tier}, 最后错误: {last_error}"
        )
        return self.get_provider(preferred_tier), preferred_tier

    # ====== 运行时动态切换：健康度评分 + runtime_reselect ======

    def record_health_check(
        self, tier: ModelTier, success: bool, response_time: float
    ) -> None:
        """记录一次 health_check 结果，更新滑动窗口与 Prometheus 健康度 Gauge。

        埋点失败不影响主流程。
        """
        if tier not in self._health_history:
            self._health_history[tier] = deque(maxlen=_HEALTH_WINDOW)
        self._health_history[tier].append((bool(success), float(response_time)))
        score = self.get_health_score(tier)
        try:
            set_provider_health_score(tier, score)
        except Exception:
            logger.exception("set_provider_health_score 埋点失败 tier=%s", tier)

    def get_health_score(self, tier: ModelTier) -> float:
        """计算某档位健康度评分（0-100）。

        评分构成：
        - 成功率（最近 _HEALTH_WINDOW 次）× 70
        - 响应速度分（平均响应时间）× 30：<1s 满分 30，<3s 得 20，<5s 得 10，否则 0
        无历史数据时返回 100（保守视为健康，待首次探测后修正）。
        """
        history = self._health_history.get(tier)
        if not history:
            return 100.0
        n = len(history)
        successes = sum(1 for s, _ in history if s)
        success_rate = successes / n
        avg_time = sum(t for _, t in history) / n
        if avg_time < 1.0:
            speed = 30.0
        elif avg_time < 3.0:
            speed = 20.0
        elif avg_time < 5.0:
            speed = 10.0
        else:
            speed = 0.0
        return success_rate * 70.0 + speed

    def runtime_reselect(
        self, current_tier: ModelTier, current_health_score: float
    ) -> ModelTier:
        """基于当前负载与健康度动态推荐档位（运行时切换）。

        规则：
        - health_score >= 60：维持当前档位
        - 30 <= health_score < 60：降级一档（L3→L2→L1→L0），避免直接跳到云端
        - health_score < 30：直接降级到 L0（云端兜底，最稳定）
        """
        if current_health_score >= 60.0:
            return current_tier
        if current_health_score < 30.0:
            return "L0"
        degrade = {"L3": "L2", "L2": "L1", "L1": "L0", "L0": "L0"}
        return degrade.get(current_tier, "L0")

    def hardware_report(self) -> Dict[str, Any]:
        """返回硬件探测报告"""
        return {
            **self._hardware,
            "recommended_tier": self.get_recommended_tier(),
            # P1: 暴露熔断器状态供 /admin/model-status 调用方观察
            "circuit_breakers": self._circuit_registry.all_states(),
        }
