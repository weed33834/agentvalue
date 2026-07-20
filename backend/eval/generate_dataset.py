"""
生成回归测试数据集

用法:
    python backend/eval/generate_dataset.py
输出:
    backend/eval/dataset.json

自 v1.1 起,dataset 改为手写 15 条双输入用例(E2001-E2015,覆盖 5 类 archetype 各 3 条),
不再由本脚本批量生成。本文件保留 generate_case/main 用于:
- 离线生成新的 archetype 模板草稿(再人工微调)
- 测试 fixture 与下游脚本(如 eval.evaluate --compare)对老格式的兼容
- 文档与回归校验

v1.0 → v1.1 变更:
- 双输入(daily_report + 次要输入)替代单输入,支持 evidence_sources 多源追溯
- 分数区间宽度放宽到 ≥10(可变),中心仍落在 archetype score_range 内
- 新增 expected_employee_view_tone 字段,标注三视图语气校验锚点
- input_id 格式: daily-{period}-{employee_id},对齐手写 dataset
"""

import json
import random
from pathlib import Path

random.seed(42)

ARCHETYPES = {
    "star": {
        "score_range": (85, 96),
        "keywords": ["超额完成", "主导", "优化", "团队", "高质量"],
        "reports": [
            "本周主导完成用户画像模块重构，性能提升40%，并辅导两名新人完成CR。",
            "提前2天交付Q3核心需求，代码Review通过率100%，客户反馈零Bug。",
            "组织技术分享一次，沉淀最佳实践文档3篇，团队采纳率80%。",
        ],
    },
    "slacker": {
        "score_range": (45, 59),
        "keywords": ["延期", "沟通不及时", "质量不高", "待改进", "未自测"],
        "reports": [
            "本周任务延期2天，日报内容简略，未主动同步阻塞问题。",
            "提交的代码未自测，导致测试环境崩溃一次，修复耗时半天。",
            "会议迟到两次，需求理解反复，输出物与预期差距较大。",
        ],
    },
    "bottleneck": {
        "score_range": (60, 74),
        "keywords": ["加班", "效率", "流程", "阻塞", "熟练度"],
        "reports": [
            "工作投入度高，经常加班，但产出低于预期，关键路径多次被阻塞。",
            "负责模块复杂度评估不足，导致排期延误，需加强技术拆解能力。",
            "沟通响应及时，但代码质量波动大，重构债务累积。",
        ],
    },
    "newcomer": {
        "score_range": (65, 78),
        "keywords": ["学习", "适应", "请教", "成长", "基础"],
        "reports": [
            "入职第二周，已完成环境搭建并独立完成2个简单Bug修复，学习主动性强。",
            "对业务逻辑理解较快，但技术栈熟练度不足，需继续积累。",
            "积极参与团队分享，日报记录详细，成长速度符合预期。",
        ],
    },
    "workaholic": {
        "score_range": (75, 85),
        "keywords": ["完成", "加班", "独立", "沟通少", "稳健"],
        "reports": [
            "本周独立完成全部指派任务，加班较多，但跨团队协作沟通偏少。",
            "输出稳定可靠，但创新性和主动性有提升空间，较少分享经验。",
            "对细节把控严格，交付质量合格，但大包大揽导致团队依赖。",
        ],
    },
}

PERIODS = [f"2026-W{i:02d}" for i in range(20, 30)]


def generate_case(idx: int) -> dict:
    """生成单条用例(旧格式:单输入 + 三位填充 input_id,宽度 10 区间)。

    v1.1 起,生产 dataset.json 是手写双输入格式,不再由本函数批量产出。
    本函数保留用于 fixture / 历史兼容 / 离线草稿生成。
    """
    archetype = random.choice(list(ARCHETYPES.keys()))
    info = ARCHETYPES[archetype]
    employee_id = f"E{1000 + idx}"
    period = random.choice(PERIODS)
    report = random.choice(info["reports"])
    expected_score = random.randint(*info["score_range"])

    # 只选择确实出现在报告中的关键词作为 expected_contains,避免 Mock/LLM 无法命中
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
        "expected_view_keys": ["summary", "growth_areas", "next_week_focus"],
    }


def main(count: int = 50) -> None:
    """生成 count 条用例并写入 dataset.json。

    注: v1.1 起,生产 dataset.json 是手写版本,请勿在生产路径调用本 main()
    覆盖手写数据。仅供离线生成草稿或测试 fixture 使用(测试用例通过
    monkeypatch 重定向 __file__ 避免覆盖生产文件)。
    """
    dataset = [generate_case(i) for i in range(1, count + 1)]
    output = Path(__file__).with_name("dataset.json")
    output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已生成 {count} 条测试用例到 {output}")
