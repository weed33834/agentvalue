"""
auth/jwt_handler.py 单元测试

覆盖：
- create_access_token 生成 token 并能解码出 sub/role/name
- 不同 role（employee/manager/hr/admin）都生成正常
- token 包含 iat/exp/jti，每次 jti 唯一
- decode_access_token 对过期 token 返回 None
- decode_access_token 对篡改 signature / 错误密钥的 token 返回 None
- decode_access_token 对非 JWT 字符串返回 None
- decode_access_token 对缺失 exp/iat 的 token 返回 None（require 强校验）
- decode_access_token 对缺失 sub 的 token 仍可解码（rbac 层单独校验 sub）
- leeway 内的轻微过期仍可解码
- extract_bearer_token 各种 header 格式
- _ensure_secret_key 缺失时 raise RuntimeError
- conftest test_settings 下 jwt_secret_key 有测试值
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from auth.jwt_handler import (
    _ensure_secret_key,
    create_access_token,
    decode_access_token,
    extract_bearer_token,
    reset_jwt_secret_cache,
)
from core.config import get_settings


# ---------------- create_access_token ----------------


class TestCreateAccessToken:
    def test_create_token_decodes_to_correct_payload(self):
        token = create_access_token("user-001", "admin", name="Alice")
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "user-001"
        assert payload["role"] == "admin"
        assert payload["name"] == "Alice"

    def test_token_has_iat_exp_jti(self):
        token = create_access_token("u", "employee")
        payload = decode_access_token(token)
        assert payload is not None
        assert "iat" in payload
        assert "exp" in payload
        assert "jti" in payload
        assert isinstance(payload["jti"], str)

    def test_default_name_is_empty_string(self):
        token = create_access_token("user-002", "employee")
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["name"] == ""

    def test_each_token_has_unique_jti(self):
        """每次生成的 token 应有不同 jti"""
        t1 = create_access_token("u", "employee")
        t2 = create_access_token("u", "employee")
        p1 = decode_access_token(t1)
        p2 = decode_access_token(t2)
        assert p1 is not None and p2 is not None
        assert p1["jti"] != p2["jti"]

    @pytest.mark.parametrize(
        "role",
        ["employee", "manager", "hr", "admin"],
    )
    def test_create_token_for_all_roles(self, role):
        token = create_access_token("u-1", role)
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["role"] == role


# ---------------- decode_access_token ----------------


class TestDecodeAccessToken:
    def test_decode_valid_token(self):
        token = create_access_token("user-x", "manager", name="Bob")
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "user-x"
        assert payload["role"] == "manager"

    def test_decode_expired_token_returns_none(self):
        """过期 token 解码返回 None（expires_minutes=-100，远超 leeway 30s）"""
        token = create_access_token("user-exp", "employee", expires_minutes=-100)
        assert decode_access_token(token) is None

    def test_decode_recently_expired_within_leeway_still_valid(self):
        """leeway（默认 30s）内的轻微过期仍可解码（容忍时钟漂移）"""
        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "u-leeway",
            "role": "employee",
            "iat": now - timedelta(seconds=10),
            "exp": now - timedelta(seconds=5),  # 5 秒前过期，在 leeway 内
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        result = decode_access_token(token)
        assert result is not None
        assert result["sub"] == "u-leeway"

    def test_decode_tampered_signature_returns_none(self):
        """篡改 signature 的 token 解码失败"""
        token = create_access_token("u", "employee")
        tail = token[-4:]
        tampered_tail = "AAAA" if tail != "AAAA" else "BBBB"
        tampered = token[:-4] + tampered_tail
        assert decode_access_token(tampered) is None

    def test_decode_token_with_wrong_secret_returns_none(self):
        """用错误密钥签发的 token 解码失败"""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "u-wrong",
            "role": "admin",
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(payload, "a-completely-different-secret", algorithm="HS256")
        assert decode_access_token(token) is None

    def test_decode_non_jwt_string_returns_none(self):
        """非 JWT 字符串解码返回 None"""
        assert decode_access_token("not.a.jwt") is None
        assert decode_access_token("random-string") is None
        assert decode_access_token("") is None

    def test_decode_token_missing_exp_returns_none(self):
        """缺失 exp claim 的 token 解码失败（require exp/iat 强校验）"""
        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "u-no-exp",
            "role": "employee",
            "iat": now,
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        assert decode_access_token(token) is None

    def test_decode_token_missing_iat_returns_none(self):
        """缺失 iat claim 的 token 解码失败"""
        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "u-no-iat",
            "role": "employee",
            "exp": now + timedelta(minutes=10),
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        assert decode_access_token(token) is None

    def test_decode_token_missing_sub_still_decodes(self):
        """缺失 sub claim 的 token 仍可解码（sub 不在 require 列表）。

        jwt_handler 层不强制 sub；缺失 sub 由 rbac 层单独校验并返回 401。
        """
        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "role": "employee",
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        result = decode_access_token(token)
        assert result is not None
        assert "sub" not in result

    def test_decode_token_missing_role_still_decodes(self):
        """缺失 role claim 的 token 仍可解码（role 不在 require 列表）。

        rbac 层会因 Role("") 抛 ValueError 而返回 401。
        """
        settings = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": "u-no-role",
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        result = decode_access_token(token)
        assert result is not None
        assert "role" not in result


# ---------------- extract_bearer_token ----------------


class TestExtractBearerToken:
    def test_valid_bearer_header(self):
        assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_lowercase_bearer(self):
        assert extract_bearer_token("bearer xyz") == "xyz"

    def test_mixed_case_bearer(self):
        assert extract_bearer_token("BeArEr mixed") == "mixed"

    def test_no_prefix_returns_none(self):
        assert extract_bearer_token("just-a-token") is None

    def test_wrong_prefix_returns_none(self):
        assert extract_bearer_token("Basic abc") is None

    def test_empty_header_returns_none(self):
        assert extract_bearer_token("") is None

    def test_none_header_returns_none(self):
        assert extract_bearer_token(None) is None

    def test_bearer_with_no_token(self):
        """只有 'Bearer ' 无 token 部分，返回空字符串"""
        assert extract_bearer_token("Bearer ") == ""

    def test_token_with_internal_spaces_preserved(self):
        """split(maxsplit=1) 保留 token 内部空格"""
        assert extract_bearer_token("Bearer abc def") == "abc def"


# ---------------- _ensure_secret_key ----------------


class TestEnsureSecretKey:
    @pytest.fixture(autouse=True)
    def _reset_jwt_cache(self):
        """H5: JWT secret 模块级缓存,每个测试前后清空避免相互影响"""
        reset_jwt_secret_cache()
        yield
        reset_jwt_secret_cache()

    def test_returns_secret_when_configured(self):
        """conftest 已配置 jwt_secret_key，应返回测试值"""
        settings = get_settings()
        key = _ensure_secret_key(settings)
        assert key == "test-only-jwt-secret-do-not-use-in-production"

    def test_raises_runtime_error_when_missing(self, monkeypatch):
        """未配置 jwt_secret_key 时应 raise RuntimeError"""
        settings = get_settings()
        monkeypatch.setattr(settings, "jwt_secret_key", None)
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
            _ensure_secret_key(settings)
