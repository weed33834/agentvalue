"""
auth/password.py 单元测试

覆盖：
- hash_password 生成与原文不同、bcrypt 格式
- 同一密码两次 hash 产生不同 salt（bcrypt 特性）
- verify_password 正确/错误密码
- verify_password 对空 hash / 损坏 hash 返回 False（容错不抛异常）
- 空字符串密码可正常 hash 与 verify
- 超长密码（>72 字节）被截断后仍可校验
- unicode / emoji 密码往返
- _truncate 按字节截断行为

注：password.py 未提供密码强度校验，故无弱口令拒绝测试。
"""

import pytest

from auth.password import _MAX_PWD_BYTES, _truncate, hash_password, verify_password


# ---------------- hash_password ----------------


class TestHashPassword:
    def test_hash_differs_from_plaintext(self):
        plain = "S3cret-Pa55!"
        hashed = hash_password(plain)
        assert hashed != plain

    def test_hash_has_bcrypt_prefix(self):
        """bcrypt hash 以 $2 开头（$2a$/$2b$/$2y$）"""
        hashed = hash_password("anything")
        assert hashed.startswith("$2")

    def test_hash_returns_str(self):
        hashed = hash_password("abc")
        assert isinstance(hashed, str)

    def test_same_password_yields_different_hash(self):
        """同一密码两次 hash 应有不同 salt（bcrypt 特性）"""
        plain = "same-password-123"
        h1 = hash_password(plain)
        h2 = hash_password(plain)
        assert h1 != h2

    def test_hash_empty_string(self):
        """空字符串密码也能正常 hash（bcrypt 允许）"""
        hashed = hash_password("")
        assert isinstance(hashed, str)
        assert verify_password("", hashed) is True

    def test_hash_unicode_and_emoji(self):
        plain = "密码🔐P@ssw0rd"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True


# ---------------- verify_password ----------------


class TestVerifyPassword:
    def test_verify_correct_password(self):
        plain = "correct-horse-battery-staple"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True

    def test_verify_wrong_password(self):
        hashed = hash_password("the-real-password")
        assert verify_password("wrong-password", hashed) is False

    def test_verify_returns_false_for_empty_hash(self):
        """hash 为空字符串时返回 False"""
        assert verify_password("anything", "") is False

    def test_verify_returns_false_for_corrupted_hash(self):
        """损坏的 hash 格式应返回 False，不抛异常"""
        assert verify_password("anything", "not-a-valid-bcrypt-hash") is False

    def test_verify_returns_false_for_garbage_bcrypt_format(self):
        """形似 bcrypt 但内容损坏的 hash 应返回 False"""
        assert verify_password("anything", "$2b$12$invalid_garbage_data!!!") is False

    def test_verify_round_trip_after_multiple_hashes(self):
        """多次 hash 同一密码，每次 verify 都应通过"""
        plain = "repeatable-pwd"
        for _ in range(3):
            assert verify_password(plain, hash_password(plain)) is True


# ---------------- _truncate（72 字节截断） ----------------


class TestTruncate:
    def test_short_password_unchanged(self):
        assert _truncate("short") == b"short"

    def test_exactly_72_bytes_unchanged(self):
        """恰好 72 字节不截断"""
        plain = "a" * _MAX_PWD_BYTES
        assert len(_truncate(plain)) == _MAX_PWD_BYTES

    def test_over_72_bytes_truncated(self):
        """超过 72 字节截断到 72"""
        plain = "a" * 100
        raw = _truncate(plain)
        assert len(raw) == _MAX_PWD_BYTES

    def test_multibyte_counts_bytes_not_chars(self):
        """中文字符 3 字节/个，按字节而非字符截断"""
        plain = "字" * 30  # 90 字节
        raw = _truncate(plain)
        assert len(raw) == _MAX_PWD_BYTES  # 72 字节


# ---------------- 超长密码（>72 字节）行为 ----------------


class TestLongPassword:
    def test_long_password_still_verifies(self):
        """超过 72 字节的密码被截断后仍能正常 verify（hash/verify 截断一致）"""
        plain = "x" * 100
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True

    def test_only_first_72_bytes_matter(self):
        """前 72 字节相同、后续不同的两个密码互相 verify 通过（截断特性）"""
        base = "a" * 72
        plain1 = base + "extra1"
        plain2 = base + "extra2"
        h1 = hash_password(plain1)
        assert verify_password(plain2, h1) is True

    def test_long_multibyte_password_verifies(self):
        """超长中文密码（>72 字节）截断后仍可 verify"""
        plain = "密码" * 50  # 300 字节
        hashed = hash_password(plain)
        assert verify_password(plain, hashed) is True
