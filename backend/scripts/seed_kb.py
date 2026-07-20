"""
公司知识库种子脚本

为本地开发或试点环境预置评分标准、价值观、培训材料等知识库文档，
供评估图 retrieve_context 节点检索与前端 KB 管理界面联调。

用法：
    cd backend
    python -m scripts.seed_kb            # 幂等插入默认文档
    python -m scripts.seed_kb --clear    # 先清空再插入
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from core.database import AsyncSessionLocal, Base, engine
from models.models import CompanyKB


# 默认知识库文档清单：覆盖评分维度、价值观、培训材料三类
DEFAULT_KB_DOCS = [
    {
        "kb_id": "kb-dim-001",
        "title": "绩效评估维度定义",
        "content": (
            "技术能力：代码质量、架构设计、技术选型与重构能力。"
            "协作能力：跨团队沟通、Code Review、新人辅导。"
            "交付质量：自测充分度、回归 Bug 控制、文档完整性。"
        ),
        "metadata": {"category": "dimension", "version": "v1"},
    },
    {
        "kb_id": "kb-value-001",
        "title": "公司价值观",
        "content": (
            "务实：以真实数据和可验证结果说话，反对做样子。"
            "成长：把每次任务当作学习机会，主动复盘。"
            "协作：优先解决阻塞，推动整体目标达成。"
        ),
        "metadata": {"category": "value", "version": "v1"},
    },
    {
        "kb_id": "kb-train-001",
        "title": "新人辅导手册",
        "content": (
            "新人辅导要点：1) 指派 mentor 并约定每周 1on1 频率；"
            "2) 首月安排熟悉代码库与发布流程的小任务；"
            "3) 鼓励提交 PR 并及时反馈 Code Review 意见。"
        ),
        "metadata": {"category": "training", "version": "v1"},
    },
]


async def seed(clear: bool = False) -> int:
    """幂等插入默认 KB 文档，返回实际新增条数"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    inserted = 0
    async with AsyncSessionLocal() as session:
        if clear:
            result = await session.execute(select(CompanyKB))
            for doc in result.scalars().all():
                await session.delete(doc)
            await session.commit()
            print("已清空 company_kb 表")

        for item in DEFAULT_KB_DOCS:
            existing = await session.execute(
                select(CompanyKB).where(CompanyKB.kb_id == item["kb_id"])
            )
            if existing.scalar_one_or_none():
                continue
            session.add(
                CompanyKB(
                    kb_id=item["kb_id"],
                    title=item["title"],
                    content=item["content"],
                    metadata_=item["metadata"],
                )
            )
            inserted += 1
        await session.commit()

    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公司知识库种子脚本")
    parser.add_argument(
        "--clear", action="store_true", help="先清空 company_kb 表再插入"
    )
    args = parser.parse_args(argv)

    inserted = asyncio.run(seed(clear=args.clear))
    if inserted:
        print(f"知识库种子完成：新增 {inserted} 条文档")
    else:
        print("知识库种子完成：所有默认文档已存在，跳过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
