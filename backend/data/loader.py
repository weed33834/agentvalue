"""
Mock 数据加载器
"""

import json
from pathlib import Path
from typing import Dict, List, Optional


class ProfileLoader:
    """员工画像数据加载器"""

    def __init__(self, data_path: Path = None):
        if data_path is None:
            data_path = Path(__file__).parent / "profiles.json"
        self.data_path = data_path
        with open(data_path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

    def list_profiles(self) -> List[Dict]:
        return self._data.get("profiles", [])

    def get_profile(self, employee_id: str) -> Optional[Dict]:
        for p in self._data.get("profiles", []):
            if p["employee_id"] == employee_id:
                return p
        return None

    def get_inputs(self, employee_id: str, period: str) -> List[Dict]:
        profile = self.get_profile(employee_id)
        if not profile:
            return []
        for entry in profile.get("inputs", []):
            if entry["period"] == period:
                return entry.get("raw_inputs", [])
        return []

    def get_latest_period(self, employee_id: str) -> Optional[str]:
        profile = self.get_profile(employee_id)
        if not profile or not profile.get("inputs"):
            return None
        return profile["inputs"][-1]["period"]
