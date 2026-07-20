"""
E2E 测试配置
"""

import pytest

from core.config import get_settings


@pytest.fixture(autouse=True, scope="module")
def e2e_demo_mode():
    """E2E 测试需要开启演示模式以使用 seed-demo-users 接口"""
    settings = get_settings()
    original_demo = settings.auth_demo_mode
    original_jwt = settings.jwt_secret_key
    settings.auth_demo_mode = True
    settings.jwt_secret_key = "test-only-jwt-secret-do-not-use-in-production"
    yield
    settings.auth_demo_mode = original_demo
    settings.jwt_secret_key = original_jwt


def pytest_collection_modifyitems(config, items):
    """为 e2e 测试添加标记"""
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
