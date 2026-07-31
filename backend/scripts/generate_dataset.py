"""
批量生成回归测试数据集（scripts 模块）

与 `eval/generate_dataset.py` 并存，二者职责区分：
- `eval/generate_dataset.py`：v1.0 旧格式生成器（5 类 archetype：star/slacker/
  bottleneck/newcomer/workaholic），保留用于 fixture 与历史兼容。
- `scripts/generate_dataset.py`（本模块）：M1 补完的批量生成器，5 类画像
  star/steady/slacker/newhire/bottleneck，额外携带 department/level 字段，
  体现多部门多职级支持。用于离线生成评估草稿与下游脚本消费。

输出格式与 eval 模块一致（单输入 daily_report），分数区间宽度恒为 10
（[score-5, score+5]），score_range 满足 ±5 落在 [0,100] 内。

用法：
    python -m scripts.generate_dataset --count 50
    python -m scripts.generate_dataset  # 默认 50 条，写入 scripts/dataset.json
"""

import argparse
import json
import random
from pathlib import Path

random.seed(42)

# ---------------------------------------------------------------------------
# 画像配置：5 类 archetype
# score_range 满足 low-5 >= 0 且 high+5 <= 100，保证 ±5 区间恒落在 [0,100]
# ---------------------------------------------------------------------------
ARCHETYPES: dict[str, dict] = {
    "star": {
        "score_range": (85, 95),
        "keywords": ["超额完成", "主导", "高质量", "团队", "优化"],
        "reports": [
            "本周主导完成用户画像模块重构，性能优化40%，并辅导两名新人完成CR。",
            "提前2天超额完成Q3核心需求，代码Review通过率100%，客户反馈零Bug。",
            "组织技术分享一次，沉淀高质量最佳实践文档3篇，团队采纳率80%。",
        ],
    },
    "steady": {
        "score_range": (70, 80),
        "keywords": ["稳定", "按时", "协作", "规范", "达标"],
        "reports": [
            "本周按时完成全部指派任务，代码规范，与团队协作顺畅。",
            "稳定推进迭代需求，输出达标，参与团队Code Review 3次。",
            "按规范交付功能模块，无重大缺陷，协作响应及时。",
        ],
    },
    "slacker": {
        "score_range": (40, 55),
        "keywords": ["延期", "未自测", "质量不高", "待改进", "沟通不及时"],
        "reports": [
            "本周任务延期2天，日报内容简略，未主动同步阻塞问题。",
            "提交的代码未自测，导致测试环境崩溃一次，质量不高。",
            "会议迟到两次，需求理解反复，输出物待改进。",
        ],
    },
    "newhire": {
        "score_range": (60, 75),
        "keywords": ["学习", "适应", "请教", "成长", "基础"],
        "reports": [
            "入职第二周，已完成环境搭建并独立完成2个简单Bug修复，学习主动性强。",
            "对业务逻辑理解较快，但技术栈熟练度不足，主动请教同事。",
            "积极参与团队分享，日报记录详细，成长速度符合基础预期。",
        ],
    },
    "bottleneck": {
        "score_range": (55, 70),
        "keywords": ["加班", "阻塞", "熟练度", "排期", "效率"],
        "reports": [
            "工作投入度高，经常加班，但产出低于预期，关键路径多次阻塞。",
            "负责模块复杂度评估不足，导致排期延误，需加强技术拆解能力。",
            "沟通响应及时，但代码质量波动大，效率有待提升。",
        ],
    },
}

# ---------------------------------------------------------------------------
# 多部门 / 多职级配置
# ---------------------------------------------------------------------------
DEPARTMENTS: list[str] = ["工程部", "产品部", "设计部", "数据部", "运营部"]
LEVELS: list[str] = ["P5", "P6", "P7", "M1", "M2"]

# ---------------------------------------------------------------------------
# 评估周期：2026-W20 ~ 2026-W29（10 周）
# ---------------------------------------------------------------------------
PERIODS: list[str] = [f"2026-W{i:02d}" for i in range(20, 30)]

# 固定常量
EXPECTED_VIEW_KEYS: list[str] = ["summary", "growth_areas", "next_week_focus"]

# archetype 轮转顺序（与 ARCHETYPES 插入顺序一致，star 居首）
_ARCHETYPE_ORDER: list[str] = list(ARCHETYPES.keys())


def generate_case(idx: int) -> dict:
    """生成单条评估用例。

    archetype 按 idx 轮转（idx % 5），保证前 5 条覆盖全部 5 类画像。
    分数区间 = [score-5, score+5]，宽度恒为 10，中心落在 archetype
    score_range 内。expected_contains 取报告中真实命中的关键词子集
    （最多 2 个），避免 Mock/LLM 无法命中。
    """
    archetype = _ARCHETYPE_ORDER[idx % len(_ARCHETYPE_ORDER)]
    info = ARCHETYPES[archetype]
    employee_id = f"E{1000 + idx}"
    period = random.choice(PERIODS)
    report = random.choice(info["reports"])
    department = random.choice(DEPARTMENTS)
    level = random.choice(LEVELS)
    expected_score = random.randint(*info["score_range"])

    # 只选择确实出现在报告中的关键词作为 expected_contains
    present_keywords = [kw for kw in info["keywords"] if kw in report]
    expected_contains = random.sample(present_keywords, k=min(2, len(present_keywords)))

    return {
        "employee_id": employee_id,
        "period": period,
        "archetype": archetype,
        "raw_inputs": [
            {
                "input_id": f"daily-{idx:03d}",
                "type": "daily_report",
                "content": report,
            }
        ],
        "expected_overall_score_range": [expected_score - 5, expected_score + 5],
        "expected_contains": expected_contains,
        "expected_view_keys": list(EXPECTED_VIEW_KEYS),
        "department": department,
        "level": level,
    }


def generate_dataset(count: int = 50) -> list[dict]:
    """批量生成 count 条评估用例。

    employee_id 按 E1001..E{1000+count} 顺序生成，archetype 按 idx 轮转
    保证覆盖全部 5 类画像。count < 0 抛 ValueError。
    """
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    return [generate_case(i) for i in range(1, count + 1)]


def main(count: int = 50) -> None:
    """生成 count 条用例并写入 scripts/dataset.json。"""
    dataset = generate_dataset(count=count)
    output = Path(__file__).with_name("dataset.json")
    output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已生成 {count} 条测试用例到 {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="批量生成回归测试数据集（scripts 模块）"
    )
    parser.add_argument("--count", type=int, default=50, help="生成用例数量（默认 50）")
    args = parser.parse_args()
    main(count=args.count)
