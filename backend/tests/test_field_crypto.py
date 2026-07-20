"""
core/field_crypto.py 单元测试

覆盖：
- encrypt / decrypt 字符串往返（AES-GCM）
- encrypt_json / decrypt_json dict 往返与三种输入兼容
- 密钥格式校验（base64 / hex / 无效）
- 降级模式（key=None 透传）
- 解密失败容错（旧明文 / 错误密钥）
- generate_key 生成的密钥可用
- 单例缓存与 reset_field_cipher_cache 强制重建
"""

import base64
import os

import pytest

from core.field_crypto import (
    FieldCipher,
    get_field_cipher,
    reset_field_cipher_cache,
)


# ---------------- 密钥工具 ----------------


def _gen_key() -> str:
    """生成一个合法的 base64 密钥（32 字节）"""
    return base64.b64encode(os.urandom(32)).decode()


def _gen_hex_key() -> str:
    """生成一个合法的 hex 密钥（32 字节）"""
    return os.urandom(32).hex()


# ---------------- encrypt / decrypt 字符串往返 ----------------


class TestStringRoundTrip:
    def test_encrypt_decrypt_roundtrip(self):
        cipher = FieldCipher(_gen_key())
        plaintext = "敏感字段：员工绩效评估 13800138000"
        ciphertext = cipher.encrypt(plaintext)
        assert ciphertext != plaintext
        assert cipher.decrypt(ciphertext) == plaintext

    def test_encrypt_returns_base64_string(self):
        """密文应为 ASCII base64 字符串，可安全存入 DB 文本列"""
        cipher = FieldCipher(_gen_key())
        ciphertext = cipher.encrypt("hello")
        # base64 字符集
        assert all(c.isalnum() or c in "+/=" for c in ciphertext)

    def test_encrypt_same_plaintext_yields_different_ciphertext(self):
        """同一明文加密两次，因随机 nonce 应产生不同密文"""
        cipher = FieldCipher(_gen_key())
        plaintext = "同一段敏感内容"
        c1 = cipher.encrypt(plaintext)
        c2 = cipher.encrypt(plaintext)
        assert c1 != c2
        # 但都能解密回原文
        assert cipher.decrypt(c1) == plaintext
        assert cipher.decrypt(c2) == plaintext

    def test_encrypt_empty_string(self):
        cipher = FieldCipher(_gen_key())
        ciphertext = cipher.encrypt("")
        assert cipher.decrypt(ciphertext) == ""

    def test_encrypt_unicode_and_emoji(self):
        cipher = FieldCipher(_gen_key())
        plaintext = "中文测试 🚀 émoji ñ üñîçødé"
        assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


# ---------------- encrypt_json / decrypt_json ----------------


class TestJsonRoundTrip:
    def test_encrypt_json_decrypt_json_roundtrip(self):
        cipher = FieldCipher(_gen_key())
        obj = {
            "harsh_assessment": "稳定但缺乏突破",
            "risk_flags": [{"level": "medium", "category": "成长瓶颈"}],
            "score": 88.5,
            "nested": {"a": [1, 2, 3]},
        }
        ciphertext = cipher.encrypt_json(obj)
        assert isinstance(ciphertext, str)
        # 密文不应包含明文片段
        assert "稳定" not in ciphertext
        assert "harsh_assessment" not in ciphertext
        # 解密回原对象（保持结构）
        decrypted = cipher.decrypt_json(ciphertext)
        assert decrypted == obj

    def test_encrypt_json_preserves_unicode(self):
        """ensure_ascii=False 保证中文不被转义为 \\uXXXX"""
        cipher = FieldCipher(_gen_key())
        ciphertext = cipher.encrypt_json({"summary": "本周表现稳定"})
        # 解密后的字符串应保留中文（非 \\u 转义）
        decrypted = cipher.decrypt_json(ciphertext)
        assert decrypted["summary"] == "本周表现稳定"

    def test_decrypt_json_accepts_dict_passthrough(self):
        """dict 输入应原样返回（兼容旧明文数据 / DB JSON 列直接反序列化）"""
        cipher = FieldCipher(_gen_key())
        obj = {"a": 1, "b": [2, 3]}
        assert cipher.decrypt_json(obj) is obj

    def test_decrypt_json_accepts_list_passthrough(self):
        """list 输入应原样返回"""
        cipher = FieldCipher(_gen_key())
        obj = [1, 2, {"a": 3}]
        assert cipher.decrypt_json(obj) is obj

    def test_decrypt_json_accepts_none(self):
        cipher = FieldCipher(_gen_key())
        assert cipher.decrypt_json(None) is None

    def test_decrypt_json_accepts_json_string_when_disabled(self):
        """未启用加密时，JSON 字符串应被 json.loads 还原为对象"""
        cipher = FieldCipher(None)  # 降级模式
        json_str = '{"a": 1, "b": "中文"}'
        result = cipher.decrypt_json(json_str)
        assert result == {"a": 1, "b": "中文"}


# ---------------- 降级模式（key=None） ----------------


class TestDisabledMode:
    def test_disabled_encrypt_passthrough(self):
        cipher = FieldCipher(None)
        assert cipher.enabled is False
        assert cipher.encrypt("敏感内容") == "敏感内容"

    def test_disabled_decrypt_passthrough(self):
        cipher = FieldCipher(None)
        assert cipher.decrypt("任意密文") == "任意密文"

    def test_disabled_encrypt_json_returns_json_string(self):
        """降级模式 encrypt_json 返回 JSON 字符串（与密文同为 str，DB 列类型一致）"""
        cipher = FieldCipher(None)
        result = cipher.encrypt_json({"a": 1})
        assert isinstance(result, str)
        assert "a" in result  # JSON 字符串
        assert result == '{"a": 1}'


# ---------------- 解密失败容错 ----------------


class TestDecryptFailureTolerance:
    def test_decrypt_non_base64_returns_original(self):
        """非 base64 输入视为明文，原样返回"""
        cipher = FieldCipher(_gen_key())
        plaintext = "这不是密文，就是普通明文"
        assert cipher.decrypt(plaintext) == plaintext

    def test_decrypt_too_short_returns_original(self):
        """长度小于 nonce+tag 的 base64 视为明文"""
        cipher = FieldCipher(_gen_key())
        # 5 字节 base64，远小于 12+16
        short = base64.b64encode(b"short").decode()
        assert cipher.decrypt(short) == short

    def test_decrypt_wrong_key_returns_original(self):
        """用错误密钥解密失败，原样返回密文（不抛异常）"""
        cipher1 = FieldCipher(_gen_key())
        cipher2 = FieldCipher(_gen_key())
        ciphertext = cipher1.encrypt("机密内容")
        # 用不同密钥解密应失败，返回原密文
        result = cipher2.decrypt(ciphertext)
        assert result == ciphertext  # 解密失败返回原值

    def test_decrypt_corrupted_ciphertext_returns_original(self):
        """篡改后的密文（tag 不匹配）解密失败，原样返回"""
        cipher = FieldCipher(_gen_key())
        ciphertext = cipher.encrypt("原始内容")
        # 篡改最后一个字符
        tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
        result = cipher.decrypt(tampered)
        # 解密失败，返回篡改后的密文
        assert result == tampered

    def test_decrypt_json_with_invalid_ciphertext_returns_original(self):
        """decrypt_json 在解密 + json.loads 都失败时返回原值"""
        cipher = FieldCipher(_gen_key())
        # 一段既非密文也非合法 JSON 的字符串
        weird = "not-a-cipher-not-json"
        result = cipher.decrypt_json(weird)
        assert result == weird


# ---------------- 密钥格式校验 ----------------


class TestKeyDecoding:
    def test_decode_key_accepts_base64(self):
        key = base64.b64encode(os.urandom(32)).decode()
        cipher = FieldCipher(key)
        assert cipher.enabled is True

    def test_decode_key_accepts_hex(self):
        key = os.urandom(32).hex()
        cipher = FieldCipher(key)
        assert cipher.enabled is True

    def test_decode_key_rejects_invalid_string(self):
        with pytest.raises(ValueError, match="32 字节"):
            FieldCipher("not-a-valid-key")

    def test_decode_key_rejects_wrong_length_base64(self):
        # 16 字节的 base64，长度不对
        short_key = base64.b64encode(os.urandom(16)).decode()
        with pytest.raises(ValueError, match="32 字节"):
            FieldCipher(short_key)

    def test_decode_key_rejects_wrong_length_hex(self):
        short_hex = os.urandom(16).hex()
        with pytest.raises(ValueError, match="32 字节"):
            FieldCipher(short_hex)

    def test_generate_key_produces_valid_base64_key(self):
        """generate_key 生成的密钥应可直接用于构造 FieldCipher"""
        key = FieldCipher.generate_key()
        assert isinstance(key, str)
        cipher = FieldCipher(key)
        assert cipher.enabled is True
        # 能正常加解密
        assert cipher.decrypt(cipher.encrypt("test")) == "test"


# ---------------- 模块级单例 get_field_cipher ----------------


class TestSingleton:
    def test_get_field_cipher_disabled_when_no_key(self, monkeypatch):
        """settings.field_encryption_key=None 时返回透传 cipher"""
        from core.config import get_settings

        monkeypatch.setattr(get_settings(), "field_encryption_key", None)
        reset_field_cipher_cache()
        cipher = get_field_cipher()
        assert cipher.enabled is False

    def test_get_field_cipher_caches_by_key(self, monkeypatch):
        """同一密钥应复用同一 FieldCipher 实例"""
        from core.config import get_settings

        key = _gen_key()
        monkeypatch.setattr(get_settings(), "field_encryption_key", key)
        reset_field_cipher_cache()
        c1 = get_field_cipher()
        c2 = get_field_cipher()
        assert c1 is c2
        assert c1.enabled is True

    def test_get_field_cipher_rebuilds_after_key_change(self, monkeypatch):
        """密钥变更后应重建 FieldCipher 实例（自动缓存失效）"""
        from core.config import get_settings

        key1 = _gen_key()
        monkeypatch.setattr(get_settings(), "field_encryption_key", key1)
        reset_field_cipher_cache()
        c1 = get_field_cipher()

        key2 = _gen_key()
        monkeypatch.setattr(get_settings(), "field_encryption_key", key2)
        c2 = get_field_cipher()
        assert c1 is not c2
        # 用旧密钥加密的内容，新密钥实例解不出来（但容错返回原值）
        ciphertext = c1.encrypt("旧密钥加密")
        assert c2.decrypt(ciphertext) == ciphertext  # 解密失败原样返回

    def test_reset_field_cipher_cache_forces_rebuild(self, monkeypatch):
        """reset_field_cipher_cache 后即使密钥未变也应重建实例"""
        from core.config import get_settings

        key = _gen_key()
        monkeypatch.setattr(get_settings(), "field_encryption_key", key)
        reset_field_cipher_cache()
        c1 = get_field_cipher()
        reset_field_cipher_cache()
        c2 = get_field_cipher()
        assert c1 is not c2
        assert c2.enabled is True

    def test_get_field_cipher_returns_enabled_when_key_set(self, monkeypatch):
        from core.config import get_settings

        monkeypatch.setattr(get_settings(), "field_encryption_key", _gen_key())
        reset_field_cipher_cache()
        cipher = get_field_cipher()
        assert cipher.enabled is True
