"""
Prompt 加载与渲染工具
使用正则表达式精确替换占位符，避免 Prompt 中的 JSON 示例被误解析。
支持版本管理：当前版本位于 prompts/{name}.md，历史快照位于 prompts/versions/{name}_v{X.Y}.md。
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class PromptLoader:
    """加载 Prompt 文件并替换变量"""

    # 当前生产基线 Prompt 版本（与 prompts/{name}.md 头部版本一致）。
    # v1.1 由 v1.0 升级：新增 few-shot 端到端示例、chain-of-thought 引导、evidence 来源校验强化。
    CURRENT_VERSION = "v1.1"

    PLACEHOLDERS = [
        "raw_inputs",
        "employee_history",
        "company_kb",
        "employee_id",
        "period",
    ]

    def __init__(self, prompts_dir: Path = None):
        if prompts_dir is None:
            prompts_dir = Path(__file__).parent.parent / "prompts"
        self.prompts_dir = prompts_dir

    def load(self, name: str) -> str:
        path = self.prompts_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt 文件不存在: {path}")
        return path.read_text(encoding="utf-8")

    def version(self, name: str) -> str:
        """从 Prompt 文件头提取版本号"""
        text = self.load(name)
        for line in text.splitlines()[:10]:
            match = re.search(r"版本[:：*\s]*\s*(v\d+\.\d+)", line)
            if match:
                return match.group(1)
        return "unknown"

    def list_versions(self, name: str) -> List[str]:
        """
        列出某 prompt 在 versions/ 目录下的所有历史版本快照。
        返回按版本号排序的列表，例如 ["v0.1", "v0.2"]。
        """
        versions_dir = self.prompts_dir / "versions"
        if not versions_dir.exists():
            return []
        versions: List[str] = []
        for path in sorted(versions_dir.glob(f"{name}_v*.md")):
            stem = path.stem  # 例如 daily_evaluation_v0.1
            suffix = stem.split("_v", 1)[-1] if "_v" in stem else ""
            if suffix:
                versions.append(f"v{suffix}")
        return versions

    def load_version(self, name: str, version: str) -> str:
        """
        加载指定版本的 Prompt 快照（位于 prompts/versions/{name}_v{X.Y}.md）。
        version 接受 "v0.1" 或 "0.1" 两种写法。
        """
        normalized = version[1:] if version.startswith("v") else version
        path = self.prompts_dir / "versions" / f"{name}_v{normalized}.md"
        if not path.exists():
            available = self.list_versions(name)
            raise FileNotFoundError(f"Prompt 版本不存在: {path}，可用版本: {available}")
        return path.read_text(encoding="utf-8")

    def render(
        self,
        name: str,
        raw_inputs: List[Dict[str, Any]],
        employee_history: List[Dict[str, Any]] = None,
        company_kb: List[Dict[str, Any]] = None,
        employee_id: str = "",
        period: str = "",
    ) -> str:
        """渲染 Prompt，替换占位符"""
        template = self.load(name)
        return self._render_template(
            template,
            raw_inputs=raw_inputs,
            employee_history=employee_history,
            company_kb=company_kb,
            employee_id=employee_id,
            period=period,
        )

    def render_version(
        self,
        name: str,
        version: str,
        raw_inputs: List[Dict[str, Any]],
        employee_history: Optional[List[Dict[str, Any]]] = None,
        company_kb: Optional[List[Dict[str, Any]]] = None,
        employee_id: str = "",
        period: str = "",
    ) -> str:
        """渲染指定版本的 Prompt，替换占位符"""
        template = self.load_version(name, version)
        return self._render_template(
            template,
            raw_inputs=raw_inputs,
            employee_history=employee_history,
            company_kb=company_kb,
            employee_id=employee_id,
            period=period,
        )

    def _render_template(
        self,
        template: str,
        raw_inputs: List[Dict[str, Any]],
        employee_history: Optional[List[Dict[str, Any]]] = None,
        company_kb: Optional[List[Dict[str, Any]]] = None,
        employee_id: str = "",
        period: str = "",
    ) -> str:
        """渲染模板，替换已知占位符，保留其他花括号"""
        values = {
            "raw_inputs": json.dumps(raw_inputs, ensure_ascii=False, indent=2),
            "employee_history": json.dumps(
                employee_history or [], ensure_ascii=False, indent=2
            ),
            "company_kb": json.dumps(company_kb or [], ensure_ascii=False, indent=2),
            "employee_id": employee_id,
            "period": period,
        }

        def replacer(match: re.Match) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))

        # 仅替换 {raw_inputs} 等已知占位符，保留其他花括号
        pattern = re.compile(r"\{(" + "|".join(self.PLACEHOLDERS) + r")\}")
        return pattern.sub(replacer, template)
