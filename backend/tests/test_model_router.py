"""
ModelRouter 单元测试
主要测试档位选择逻辑，避免真实网络调用。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.config import Settings
from core.model_router import ModelRouter


def test_cloud_fallback_when_no_local_ram():
    """内存极低时应回退云端 L0"""
    settings = Settings(model_tier="auto")
    router = ModelRouter(settings)
    # 强制覆盖硬件检测结果为无资源
    router._hardware = {"vram_gb": 0, "ram_gb": 1, "gpu_count": 0, "gpu_names": []}
    assert router.get_recommended_tier() == "L0"


def test_l1_when_min_ram():
    """仅有 4GB 内存时选择 L1"""
    settings = Settings(model_tier="auto")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 0, "ram_gb": 4, "gpu_count": 0, "gpu_names": []}
    assert router.get_recommended_tier() == "L1"


def test_l2_when_modest_gpu():
    """有 8GB 显存 + 16GB 内存时选择 L2"""
    settings = Settings(model_tier="auto")
    router = ModelRouter(settings)
    router._hardware = {
        "vram_gb": 8,
        "ram_gb": 16,
        "gpu_count": 1,
        "gpu_names": ["RTX 4060"],
    }
    assert router.get_recommended_tier() == "L2"


def test_l3_when_high_end_gpu():
    """有 24GB 显存 + 32GB 内存时选择 L3"""
    settings = Settings(model_tier="auto")
    router = ModelRouter(settings)
    router._hardware = {
        "vram_gb": 24,
        "ram_gb": 32,
        "gpu_count": 1,
        "gpu_names": ["RTX 4090"],
    }
    assert router.get_recommended_tier() == "L3"


def test_manual_tier_override():
    """手动设置档位时优先使用手动设置"""
    settings = Settings(model_tier="L1")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 24, "ram_gb": 32, "gpu_count": 1, "gpu_names": []}
    assert router.get_recommended_tier() == "L1"


def test_provider_config_for_cloud():
    """L0 档位应生成云端 Provider 配置"""
    settings = Settings(
        model_tier="L0",
        openai_api_key="fake-key",
        openai_model="gpt-4o-mini",
    )
    router = ModelRouter(settings)
    provider = router.get_provider("L0")
    assert provider.config.model_name == "gpt-4o-mini"
    assert provider.config.base_url == settings.openai_base_url


def test_provider_config_for_local():
    """L2 档位应生成本地 Provider 配置"""
    settings = Settings(model_tier="L2", local_base_url="http://localhost:1234/v1")
    router = ModelRouter(settings)
    provider = router.get_provider("L2")
    assert provider.config.model_name == settings.local_model_l2
    assert provider.config.base_url == "http://localhost:1234/v1"


def test_provider_config_injects_embedding_model():
    """get_provider 应把 Settings.embedding_model 注入 ProviderConfig"""
    settings = Settings(
        model_tier="L0",
        openai_api_key="fake-key",
        embedding_model="text-embedding-3-large",
    )
    router = ModelRouter(settings)
    provider = router.get_provider("L0")
    assert provider.config.embedding_model == "text-embedding-3-large"
    # OpenAICompatibleProvider 应暴露该模型名
    assert provider.embedding_model == "text-embedding-3-large"


# ---------------- runtime_reselect：基于健康度的运行时动态切换 ----------------


def test_runtime_reselect_keeps_tier_when_healthy():
    """health_score >= 60 时维持当前档位（含边界 60）"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    assert router.runtime_reselect("L2", 80.0) == "L2"
    assert router.runtime_reselect("L3", 60.0) == "L3"  # 边界值包含


def test_runtime_reselect_degrades_one_step_when_medium():
    """30 <= health_score < 60 时降级一档"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    assert router.runtime_reselect("L3", 50.0) == "L2"
    assert router.runtime_reselect("L2", 40.0) == "L1"
    assert router.runtime_reselect("L1", 30.0) == "L0"  # 边界值包含
    # L0 已是最低，再降仍为 L0
    assert router.runtime_reselect("L0", 35.0) == "L0"


def test_runtime_reselect_drops_to_l0_when_critical():
    """health_score < 30 时直接降到 L0（云端兜底）"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    assert router.runtime_reselect("L3", 25.0) == "L0"
    assert router.runtime_reselect("L2", 10.0) == "L0"
    assert router.runtime_reselect("L0", 5.0) == "L0"


# ---------------- 健康度评分 ----------------


def test_get_health_score_defaults_to_100_without_history():
    """无 health_check 历史时返回 100（保守视为健康）"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    assert router.get_health_score("L2") == 100.0


def test_record_health_check_updates_score():
    """记录后评分应反映成功率与响应速度"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    # 9 次成功 + 1 次失败，响应时间均 0.5s
    for _ in range(9):
        router.record_health_check("L1", success=True, response_time=0.5)
    router.record_health_check("L1", success=False, response_time=0.5)
    score = router.get_health_score("L1")
    # 成功率 0.9 × 70 + 速度 30(<1s) = 93
    assert score == pytest.approx(0.9 * 70 + 30, rel=1e-3)


def test_record_health_check_window_caps_at_10():
    """滑动窗口只保留最近 10 次，旧记录被挤出"""
    settings = Settings(model_tier="auto", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    # 先记 10 次失败（响应慢，>= 5s 触发 speed=0）
    for _ in range(10):
        router.record_health_check("L0", success=False, response_time=10.0)
    # 再记 1 次成功（快速），应挤出 1 个旧失败记录
    router.record_health_check("L0", success=True, response_time=0.5)
    assert len(router._health_history["L0"]) == 10
    # 评分应反映：1 成功 + 9 失败，avg_time = (9*10 + 0.5)/10 = 9.05s >= 5s → speed=0
    score = router.get_health_score("L0")
    # 成功率 0.1 × 70 + 速度 0(avg>=5s) = 7
    assert score == pytest.approx(0.1 * 70 + 0, rel=1e-3)


# ---------------- 增强后的硬件探测 ----------------


def test_detect_hardware_includes_cpu_disk_network_keys():
    """_detect_hardware 静态调用（无 cloud_base_url）应跳过网络但采集 CPU/磁盘"""
    hw = ModelRouter._detect_hardware()  # 不传 cloud_base_url → 跳过网络探测
    assert "cpu_count" in hw
    assert "disk_free_gb" in hw
    assert "network_latency_s" in hw
    # 未提供 cloud_base_url 时不做网络探测
    assert hw["network_latency_s"] is None
    # 保留原有字段
    assert "vram_gb" in hw
    assert "ram_gb" in hw
    assert hw["cpu_count"] >= 0
    assert hw["disk_free_gb"] >= 0


# ---------------- get_provider_with_fallback 健康度记录 ----------------


def _mock_provider(healthy=True, raises=False):
    """构造 mock provider，health_check 返回 healthy 或抛异常"""
    p = MagicMock()
    if raises:
        p.health_check = AsyncMock(side_effect=RuntimeError("unreachable"))
    else:
        p.health_check = AsyncMock(return_value=healthy)
    return p


async def test_fallback_records_health_check_history():
    """get_provider_with_fallback 应对每次 health_check 计时并写入健康度滑动窗口"""
    # 用 closed port 的 cloud_base_url，使 __init__ 中的网络探测立即失败、不阻塞
    settings = Settings(model_tier="L1", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    router._hardware = {"vram_gb": 0, "ram_gb": 4, "gpu_count": 0, "gpu_names": []}
    assert router.get_recommended_tier() == "L1"

    # 首选 L1 不健康，L0 健康 → 降级到 L0，两个档位都会被记录
    def fake_get_provider(tier):
        return _mock_provider(healthy=(tier == "L0"))

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == "L0"

    # L1 健康检查失败被记录（success=False）
    assert len(router._health_history["L1"]) == 1
    success, elapsed = router._health_history["L1"][0]
    assert success is False
    assert elapsed >= 0.0
    # L0 健康检查成功被记录（success=True）
    assert len(router._health_history["L0"]) == 1
    assert router._health_history["L0"][0][0] is True
    # 失败记录使 L1 评分下降（< 100，区别于无历史的默认 100）
    assert router.get_health_score("L1") < 100.0


async def test_fallback_records_failed_health_check_on_exception():
    """health_check 抛异常时应记录 success=False，且评分下降"""
    settings = Settings(model_tier="L3", cloud_base_url="http://127.0.0.1:1")
    router = ModelRouter(settings)
    router._hardware = {
        "vram_gb": 24,
        "ram_gb": 32,
        "gpu_count": 1,
        "gpu_names": ["RTX 4090"],
    }

    def fake_get_provider(tier):
        if tier == "L3":
            return _mock_provider(raises=True)
        return _mock_provider(healthy=(tier == "L0"))

    router.get_provider = fake_get_provider  # type: ignore
    provider, tier = await router.get_provider_with_fallback()
    assert tier == "L0"
    # L3 异常被记录为失败
    assert len(router._health_history["L3"]) == 1
    success, _ = router._health_history["L3"][0]
    assert success is False
