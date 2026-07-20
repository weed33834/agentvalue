"""
core/model_router.py 补充单元测试
覆盖：_detect_hardware 的 psutil/torch/nvidia-smi 各分支、get_provider 云端缺 key 告警、
      get_provider_with_fallback 降级链、hardware_report。
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import Settings
from core.model_router import ModelRouter


# ---------------- _detect_hardware ----------------


def _make_fake_torch(device_count=1, vram_gb=8.0, name="RTX 4060", cuda_available=True):
    """构造一个 fake torch 模块"""
    fake = types.ModuleType("torch")

    class _Props:
        total_memory = int(vram_gb * 1024**3)

    class _Cuda:
        @staticmethod
        def is_available():
            return cuda_available

        @staticmethod
        def device_count():
            return device_count

        @staticmethod
        def get_device_properties(i):
            _Props.name = name  # type: ignore[attr-defined]
            return _Props()

    fake.cuda = _Cuda()
    return fake


def test_detect_hardware_with_torch_cuda(monkeypatch):
    """torch.cuda 可用时应正确探测显存与 GPU 名称"""
    fake_torch = _make_fake_torch(device_count=2, vram_gb=12.0, name="A100")
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    hw = ModelRouter._detect_hardware()
    assert hw["gpu_count"] == 2
    assert hw["vram_gb"] == pytest.approx(24.0, rel=1e-3)
    assert hw["gpu_names"] == ["A100", "A100"]


def test_detect_hardware_psutil_failure(monkeypatch, caplog):
    """psutil.virtual_memory 抛异常时应跳过内存检测并记录 warning"""
    fake_psutil = types.ModuleType("psutil")

    @staticmethod
    def _boom():
        raise RuntimeError("psutil boom")

    fake_psutil.virtual_memory = _boom
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    # torch 不存在时自然走 nvidia-smi 回退，这里也排除掉 nvidia-smi 使其进入最终 except
    import core.model_router as mr

    monkeypatch.setattr(
        mr.subprocess,
        "check_output",
        MagicMock(side_effect=FileNotFoundError("no nvidia-smi")),
    )

    with caplog.at_level("WARNING", logger="core.model_router"):
        hw = ModelRouter._detect_hardware()
    assert hw["ram_gb"] == 0.0
    assert hw["gpu_count"] == 0
    assert any("无法检测内存" in r.message for r in caplog.records)


def test_detect_hardware_nvidia_smi_fallback(monkeypatch):
    """torch 不可用时通过 nvidia-smi 解析显存与 GPU 名称"""
    # 确保 torch 不可导入（环境本就无 torch，显式移除以防污染）
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    nvidia_output = "Tesla T4, 15360 MiB\nTesla V100, 32510 MiB\n"
    import core.model_router as mr

    monkeypatch.setattr(
        mr.subprocess,
        "check_output",
        MagicMock(return_value=nvidia_output),
    )
    hw = ModelRouter._detect_hardware()
    assert hw["gpu_count"] == 2
    assert hw["gpu_names"] == ["Tesla T4", "Tesla V100"]
    assert hw["vram_gb"] == pytest.approx((15360 + 32510) / 1024, rel=1e-3)
    assert hw["ram_gb"] > 0  # psutil 正常工作


def test_detect_hardware_no_gpu_at_all(monkeypatch):
    """torch 与 nvidia-smi 均不可用时应返回零显存且记录 warning"""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    import core.model_router as mr

    monkeypatch.setattr(
        mr.subprocess, "check_output", MagicMock(side_effect=FileNotFoundError("none"))
    )
    hw = ModelRouter._detect_hardware()
    assert hw["vram_gb"] == 0.0
    assert hw["gpu_count"] == 0
    assert hw["gpu_names"] == []
    assert hw["ram_gb"] > 0


# ---------------- get_provider 云端缺 key 告警 ----------------


def test_cloud_provider_warns_when_no_api_key(monkeypatch, caplog):
    """L0 档位未配置 api_key 时应记录告警，ProviderConfig.api_key 为 None"""

    # Mock 掉真实 Provider 构造，避免 AsyncOpenAI 缺凭据抛错（告警在构造前记录）
    class _DummyProvider:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr("core.model_router.OpenAICompatibleProvider", _DummyProvider)
    settings = Settings(model_tier="L0")
    assert not (settings.cloud_api_key or settings.openai_api_key)
    router = ModelRouter(settings)
    with caplog.at_level("WARNING", logger="core.model_router"):
        provider = router.get_provider("L0")
    assert provider is not None
    # ProviderConfig 中的 api_key 仍为 None（settings 未配置）
    assert provider.config.api_key is None
    assert any("未配置" in r.message for r in caplog.records)


# ---------------- hardware_report ----------------


def test_hardware_report_includes_recommended_tier():
    """hardware_report 应包含硬件信息与推荐档位"""
    settings = Settings(model_tier="auto")
    router = ModelRouter(settings)
    router._hardware = {
        "vram_gb": 8,
        "ram_gb": 16,
        "gpu_count": 1,
        "gpu_names": ["RTX"],
    }
    report = router.hardware_report()
    assert report["recommended_tier"] == "L2"
    assert report["gpu_count"] == 1
    assert report["vram_gb"] == 8
    assert report["ram_gb"] == 16


# ---------------- get_provider_with_fallback ----------------


def _mock_provider(healthy=True, raises=False):
    """构造 mock provider，health_check 返回 healthy 或抛异常"""
    p = MagicMock()
    if raises:
        p.health_check = AsyncMock(side_effect=RuntimeError("unreachable"))
    else:
        p.health_check = AsyncMock(return_value=healthy)
    return p


async def test_fallback_returns_preferred_when_healthy():
    """首选档位健康检查通过时直接返回首选"""
    settings = Settings(model_tier="L1")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 0, "ram_gb": 4, "gpu_count": 0, "gpu_names": []}
    assert router.get_recommended_tier() == "L1"

    seen_tiers = []

    def fake_get_provider(tier):
        seen_tiers.append(tier)
        # 首选 L1 健康
        return _mock_provider(healthy=(tier == "L1"))

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == "L1"
    assert seen_tiers[0] == "L1"  # 首选优先尝试


async def test_fallback_degrades_to_next_healthy_tier():
    """首选不可用时应按降级顺序尝试，命中第一个健康档位"""
    settings = Settings(model_tier="L3")  # 首选 L3
    router = ModelRouter(settings)
    router._hardware = {
        "vram_gb": 24,
        "ram_gb": 32,
        "gpu_count": 1,
        "gpu_names": ["RTX 4090"],
    }
    assert router.get_recommended_tier() == "L3"

    def fake_get_provider(tier):
        # L3/L0 不可用，L2 可用
        return _mock_provider(healthy=(tier == "L2"))

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == "L2"


async def test_fallback_all_unhealthy_returns_preferred():
    """所有档位健康检查均失败（返回 False）时应回退首选档位"""
    settings = Settings(model_tier="L1")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 0, "ram_gb": 4, "gpu_count": 0, "gpu_names": []}
    preferred = router.get_recommended_tier()

    def fake_get_provider(tier):
        return _mock_provider(healthy=False)

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == preferred


async def test_fallback_health_check_exception_falls_through():
    """某档位 health_check 抛异常时应被捕获并继续尝试下一档位"""
    settings = Settings(model_tier="L3")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 24, "ram_gb": 32, "gpu_count": 1, "gpu_names": []}

    def fake_get_provider(tier):
        if tier == "L3":
            return _mock_provider(raises=True)
        return _mock_provider(healthy=(tier == "L0"))

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == "L0"


async def test_fallback_fallback_order_starts_with_preferred():
    """降级顺序应以首选开头，并依次尝试 L0/L3/L2/L1 中尚未尝试的档位"""
    settings = Settings(model_tier="L2")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 8, "ram_gb": 16, "gpu_count": 1, "gpu_names": []}
    tried = []

    def fake_get_provider(tier):
        tried.append(tier)
        # 全部不健康，迫使遍历完整降级顺序
        return _mock_provider(healthy=False)

    router.get_provider = fake_get_provider  # type: ignore
    await router.get_provider_with_fallback()
    # 第一个尝试的必须是首选 L2
    assert tried[0] == "L2"
    # 预期顺序：首选 L2 + fallback_order[L0, L3, L2(skip), L1]，全部失败后末尾再回退首选 L2
    assert tried == ["L2", "L0", "L3", "L1", "L2"]
