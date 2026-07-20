"""
core/utils/pii.py 单元测试

覆盖:
- redact_pii: 手机号 / 邮箱 / 身份证号 / 银行卡号 四类 PII 的脱敏
- redact_dict: 嵌套 dict / list / tuple 中字符串值的递归脱敏
- redact_audit_details: 审计 details 便捷封装与 redact_dict 等价
- 边界:None / 空串 / 无 PII 文本 / 非字符串类型原样返回
"""

import pytest

from core.utils import redact_audit_details, redact_dict, redact_pii
from core.utils.pii import redact_pii as redact_pii_direct


# ---------------- redact_pii: 手机号 ----------------


class TestPhoneRedaction:
    def test_china_mobile_phone(self):
        assert redact_pii("联系我 13800138000 谢谢") == "联系我 138****8000 谢谢"

    def test_phone_at_string_boundary(self):
        assert redact_pii("13800138000") == "138****8000"

    def test_phone_in_sentence(self):
        text = "员工电话是13912345678,可以联系"
        assert redact_pii(text) == "员工电话是139****5678,可以联系"

    def test_multiple_phones(self):
        text = "13800138000 与 15912345678 都是手机号"
        assert redact_pii(text) == "138****8000 与 159****5678 都是手机号"

    def test_not_a_phone_too_short(self):
        # 10 位数字不构成手机号
        assert redact_pii("1234567890") == "1234567890"

    def test_not_a_phone_wrong_prefix(self):
        # 1[0-2] 开头不是中国手机号段
        assert redact_pii("12012345678") == "12012345678"

    def test_phone_not_redacted_when_surrounded_by_digits(self):
        # 前后紧跟数字时不应匹配(避免误报长串数字)
        assert redact_pii("9138001380009") == "9138001380009"


# ---------------- redact_pii: 邮箱 ----------------


class TestEmailRedaction:
    def test_simple_email(self):
        assert redact_pii("contact@example.com") == "co***@example.com"

    def test_email_in_sentence(self):
        text = "请发邮件到 zhang.san@company.cn 谢谢"
        assert redact_pii(text) == "请发邮件到 zh***@company.cn 谢谢"

    def test_plus_addressing_email(self):
        assert redact_pii("user+tag@gmail.com") == "us***@gmail.com"

    def test_multiple_emails(self):
        text = "alice@a.com 与 bob@b.org"
        result = redact_pii(text)
        assert "al***@a.com" in result
        assert "bo***@b.org" in result


# ---------------- redact_pii: 身份证号 ----------------


class TestIdCardRedaction:
    def test_18_digit_idcard_with_X(self):
        assert redact_pii("11010119900307123X") == "110101********123X"

    def test_18_digit_idcard_with_x(self):
        assert redact_pii("11010119900307123x") == "110101********123x"

    def test_18_digit_idcard_all_digits(self):
        assert redact_pii("350425197701010001") == "350425********0001"

    def test_idcard_in_sentence(self):
        text = "身份证号 110101199003071234 已登记"
        assert redact_pii(text) == "身份证号 110101********1234 已登记"

    def test_not_idcard_too_short(self):
        # 15 位连续数字既不够身份证号(18 位)也不够银行卡号(16~19 位),应原样返回
        assert redact_pii("123456789012345") == "123456789012345"


# ---------------- redact_pii: 银行卡号 ----------------


class TestBankCardRedaction:
    def test_19_digit_bankcard(self):
        # 6228 4804 0256 4890 018 (19 位)
        assert redact_pii("6228480402564890018") == "6228********0018"

    def test_19_digit_bankcard_alt(self):
        assert redact_pii("6227007200123456789") == "6227********6789"

    def test_16_digit_bankcard(self):
        # 16 位卡号:6225 8801 2345 6789
        assert redact_pii("6225880123456789") == "6225********6789"

    def test_bankcard_in_sentence(self):
        text = "工资卡号 6228480402564890018 已绑定"
        assert redact_pii(text) == "工资卡号 6228********0018 已绑定"

    def test_not_bankcard_too_short(self):
        # 15 位连续数字不算银行卡号(低于 16 位下限)
        assert redact_pii("123456789012345") == "123456789012345"


# ---------------- redact_pii: 综合与边界 ----------------


class TestCombinedAndEdgeCases:
    def test_mixed_pii_types(self):
        text = "张三 13800138000 zhangsan@example.com 110101199003071234"
        result = redact_pii(text)
        assert "138****8000" in result
        assert "zh***@example.com" in result
        assert "110101********1234" in result

    def test_none_returns_none(self):
        assert redact_pii(None) is None

    def test_empty_string_returns_empty(self):
        assert redact_pii("") == ""

    def test_no_pii_text_unchanged(self):
        text = "今天天气不错,只是个普通句子。"
        assert redact_pii(text) == text

    def test_chinese_text_no_false_positive(self):
        text = "评估结果: 总分 85 分, 排名第 3"
        assert redact_pii(text) == text

    def test_numbers_below_phone_threshold_unchanged(self):
        # 11 位但前缀不是 1[3-9]
        assert redact_pii("10000000000") == "10000000000"

    def test_direct_import_works(self):
        # 验证 __init__.py 导出的函数与直接 import 一致
        assert redact_pii is redact_pii_direct


# ---------------- redact_dict: 嵌套结构 ----------------


class TestRedactDict:
    def test_flat_dict_string_values(self):
        d = {"phone": "13800138000", "name": "张三"}
        result = redact_dict(d)
        assert result == {"phone": "138****8000", "name": "张三"}
        # 原 dict 不被修改
        assert d == {"phone": "13800138000", "name": "张三"}

    def test_nested_dict(self):
        d = {
            "outer": "邮箱 test@example.com",
            "inner": {"contact": "电话 13912345678"},
        }
        result = redact_dict(d)
        assert result == {
            "outer": "邮箱 te***@example.com",
            "inner": {"contact": "电话 139****5678"},
        }

    def test_list_of_strings(self):
        d = ["13800138000", "普通文本", "test@x.com"]
        result = redact_dict(d)
        assert result == ["138****8000", "普通文本", "te***@x.com"]

    def test_nested_list_in_dict(self):
        d = {"contacts": ["13800138000", "zhangsan@x.com"], "count": 2}
        result = redact_dict(d)
        assert result == {"contacts": ["138****8000", "zh***@x.com"], "count": 2}

    def test_tuple_supported(self):
        # tuple 也应递归处理(返回 tuple)
        d = ("13800138000", "test@x.com")
        result = redact_dict(d)
        assert result == ("138****8000", "te***@x.com")
        assert isinstance(result, tuple)

    def test_non_string_values_preserved(self):
        d = {
            "count": 42,
            "score": 85.5,
            "active": True,
            "data": None,
            "tags": ["a", 1, True, None],
        }
        result = redact_dict(d)
        assert result == {
            "count": 42,
            "score": 85.5,
            "active": True,
            "data": None,
            "tags": ["a", 1, True, None],
        }

    def test_none_returns_none(self):
        assert redact_dict(None) is None

    def test_empty_dict(self):
        assert redact_dict({}) == {}

    def test_empty_list(self):
        assert redact_dict([]) == []

    def test_string_directly(self):
        assert redact_dict("13800138000") == "138****8000"

    def test_int_unchanged(self):
        assert redact_dict(42) == 42

    def test_audit_log_details_realistic_shape(self):
        """模拟审计日志 details 真实形态:多层嵌套 + PII 散落"""
        details = {
            "actor": "manager_zhang",
            "action": "approve_evaluation",
            "context": {
                "employee_phone": "13800138000",
                "employee_email": "lisi@company.cn",
                "raw_input_excerpt": "我是李四 13800138000 身份证 110101199003071234",
                "metadata": {"ip": "10.0.0.1", "user_agent": "curl/8.0"},
            },
            "tags": ["approved", "manager_review"],
        }
        result = redact_dict(details)
        assert result["actor"] == "manager_zhang"
        assert result["context"]["employee_phone"] == "138****8000"
        assert result["context"]["employee_email"] == "li***@company.cn"
        assert "138****8000" in result["context"]["raw_input_excerpt"]
        assert "110101********1234" in result["context"]["raw_input_excerpt"]
        # 非 PII 字段原样保留
        assert result["context"]["metadata"]["ip"] == "10.0.0.1"
        assert result["tags"] == ["approved", "manager_review"]


# ---------------- redact_audit_details 便捷封装 ----------------


class TestRedactAuditDetails:
    def test_equivalent_to_redact_dict(self):
        details = {"phone": "13800138000", "name": "张三"}
        assert redact_audit_details(details) == redact_dict(details)

    def test_none_input(self):
        assert redact_audit_details(None) is None

    def test_empty_dict(self):
        assert redact_audit_details({}) == {}

    def test_pii_redacted(self):
        details = {"actor_phone": "13912345678", "comment": "联系 13800138000"}
        result = redact_audit_details(details)
        assert result["actor_phone"] == "139****5678"
        assert result["comment"] == "联系 138****8000"
