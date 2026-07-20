"""
Mock 员工画像数据加载测试
"""

from pathlib import Path

from data.loader import ProfileLoader


def test_load_profiles():
    loader = ProfileLoader()
    profiles = loader.list_profiles()
    assert len(profiles) == 5
    archetypes = {p["archetype"] for p in profiles}
    assert archetypes == {"workhorse", "slacker", "star", "newbie", "stuck"}


def test_get_profile():
    loader = ProfileLoader()
    profile = loader.get_profile("E1003")
    assert profile is not None
    assert profile["name"] == "明星型"
    assert profile["archetype"] == "star"


def test_get_inputs():
    loader = ProfileLoader()
    inputs = loader.get_inputs("E1002", "2026-W25")
    assert len(inputs) >= 1
    assert inputs[0]["type"] == "daily_report"


def test_get_latest_period():
    loader = ProfileLoader()
    period = loader.get_latest_period("E1001")
    assert period == "2026-W25"
