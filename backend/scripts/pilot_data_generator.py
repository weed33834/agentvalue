#!/usr/bin/env python3
"""
AgentValue-AI 多规模公司试点数据生成器

按 5 个规模档位生成试点用的员工清单与 4 周日报数据，覆盖从初创小公司
到阿里/腾讯/谷歌这种超大型企业的真实场景，并埋入若干"复杂场景"用于
评估系统的鲁棒性（双线汇报冲突、跨国时区、矩阵借调、官僚层、361 强制分布）。

数据规模：
    - 初创型 startup：15 人，全员生成
    - 成长型 growth：80 人，全员生成
    - 中型    medium：500 人，抽样 100 人
    - 大型    large ：5000 人，抽样 150 人
    - 超大型  huge  ：50000 人，抽样 200 人

每个公司输出到 {output}/{scale}/ 目录：
    - employees.json               员工清单
    - weekly_reports_week1.json    第 1 周日报（每员工 5 个工作日）
    - weekly_reports_week2.json    第 2 周
    - weekly_reports_week3.json    第 3 周
    - weekly_reports_week4.json    第 4 周

用法：
    cd backend
    python -m scripts.pilot_data_generator --scale startup --output data/pilot/
    python -m scripts.pilot_data_generator --scale all --output data/pilot/
"""

import argparse
import json
import random
import sys
import zlib
from pathlib import Path
from typing import Optional

# 兼容 `python scripts/xxx.py` 直接执行：将 backend 根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# 5 个规模档位配置
# ---------------------------------------------------------------------------
SCALE_CONFIGS: dict[str, dict] = {
    "startup": {
        "label": "初创型",
        "total_employees": 15,
        "sampled_employees": 15,  # 全员
        "departments": ["工程团队"],
        "locations": ["北京"],
        "max_level": 2,  # 2 级：员工 + 1 主管
        "structure": "flat",
        "features": [],  # 无特殊场景
    },
    "growth": {
        "label": "成长型",
        "total_employees": 80,
        "sampled_employees": 80,
        "departments": ["研发部", "产品部", "运营部"],
        "locations": ["北京"],
        "max_level": 3,
        "structure": "team",
        "features": [],
    },
    "medium": {
        "label": "中型",
        "total_employees": 500,
        "sampled_employees": 100,  # 抽样
        "departments": [
            "技术研发部",
            "产品设计部",
            "运营增长部",
            "市场部",
            "销售部",
            "客户成功部",
            "人力资源部",
            "财务管理部",
        ],
        "locations": ["北京", "上海", "深圳"],
        "max_level": 4,
        "structure": "department",
        "features": ["cross_location"],
    },
    "large": {
        "label": "大型",
        "total_employees": 5000,
        "sampled_employees": 150,
        "departments": [
            "电商技术部",
            "搜索推荐部",
            "数据平台部",
            "基础架构部",
            "算法工程部",
            "国际化业务部",
            "客户体验部",
            "商业化部",
            "支付风控部",
            "安全合规部",
            "增长黑客部",
            "内容生态部",
            "云业务部",
            "ToB 解决方案部",
            "智能客服部",
            "物流技术部",
            "财务技术部",
            "HR Tech",
            "IT 基础设施部",
            "测试质量部",
            "研发效能部",
        ],
        "locations": ["北京", "上海", "深圳", "新加坡", "旧金山"],
        "max_level": 5,
        "structure": "matrix",  # 矩阵式
        "features": ["cross_location", "matrix_secondment", "bureaucratic_layer"],
    },
    "huge": {
        "label": "超大型",
        "total_employees": 50000,
        "sampled_employees": 200,
        "departments": [
            "淘宝技术部",
            "天猫技术部",
            "云智能事业群",
            "本地生活事业部",
            "国际电商事业部",
            "菜鸟网络部",
            "蚂蚁集团技术部",
            "达摩院",
            "阿里妈妈部",
            "钉钉事业部",
            "飞猪事业部",
            "大文娱技术部",
            "OA 企业智联部",
            "新零售技术部",
            "盒马技术部",
            "饿了么技术部",
            "优酷技术部",
            "夸克事业部",
            "平头哥半导体",
            "达摩院语言技术",
            "淘宝直播部",
            "闲鱼技术部",
            "淘特事业部",
            "社区团购事业部",
            "出海业务部-东南亚",
            "出海业务部-欧洲",
            "出海业务部-北美",
            "阿里云国际部",
            "阿里云企业服务部",
            "阿里云数据库部",
            "阿里云网络部",
            "阿里云安全部",
            "阿里云存储部",
            "AI 平台部",
            "大数据部",
            "机器学习平台部",
            "客服体验事业群",
            "商家平台部",
            "B 类业务部",
            "采购平台部",
            "财务中台部",
            "HR 中台部",
            "法务合规部",
            "战略投资部",
            "总裁办技术组",
            "技术风险部",
            "质量保障中台",
            "工程效能部",
            "开源办公室",
            "技术公益部",
        ],
        "locations": [
            "北京",
            "上海",
            "深圳",
            "杭州",
            "新加坡",
            "旧金山",
            "西雅图",
            "伦敦",
        ],
        "max_level": 6,
        "structure": "bu",  # BU 制
        "features": [
            "cross_location",
            "matrix_secondment",
            "bureaucratic_layer",
            "dual_line_reporting",
            "force_361",
        ],
    },
}

WEEKS = 4
WORKDAYS_PER_WEEK = 5

# 中文姓名素材（保持小而真实，避免每次生成差异过大）
SURNAMES = "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭"
GIVEN_NAMES_M = [
    "伟",
    "强",
    "磊",
    "勇",
    "杰",
    "涛",
    "明",
    "超",
    "鹏",
    "辉",
    "斌",
    "波",
    "宇",
    "凯",
    "晨",
]
GIVEN_NAMES_F = [
    "芳",
    "娜",
    "敏",
    "静",
    "丽",
    "娟",
    "婷",
    "雪",
    "倩",
    "燕",
    "颖",
    "璐",
    "薇",
    "雯",
    "婷",
]
EN_NAMES = [
    "Kevin",
    "Lisa",
    "David",
    "Amy",
    "Tom",
    "Sarah",
    "Michael",
    "Jessica",
    "Chris",
    "Emma",
]

# 职级体系：P 系列工程师 + M 系列管理者
LEVELS_BY_TIER = {
    2: ["P5", "P6", "M1"],
    3: ["P5", "P6", "P7", "M1", "M2"],
    4: ["P5", "P6", "P7", "P8", "M1", "M2", "M3"],
    5: ["P5", "P6", "P7", "P8", "P9", "M1", "M2", "M3", "M4"],
    6: ["P4", "P5", "P6", "P7", "P8", "P9", "P10", "M1", "M2", "M3", "M4", "M5"],
}

# 海外办公地（日报需中英文混合）
OVERSEAS_LOCATIONS = {"新加坡", "旧金山", "西雅图", "伦敦"}

# 日报场景模板：覆盖正常绩效、加班、协作、新人培养、技术债、线上故障、客户投诉、晋升答辩等
SCENARIO_TEMPLATES = [
    {
        "tag": "normal",
        "work": "推进 {task}，进度 {pct}%，已合入主干分支",
        "collab": "与产品同学对齐需求细节，确认验收标准",
        "output": "PR #{pr} 合并，单元测试覆盖率 {cov}%",
        "weight": 30,
    },
    {
        "tag": "overtime",
        "work": "通宵处理 {task} 紧急修复，21:00 上线灰度",
        "collab": "与运维、SRE 协同排查回滚方案",
        "output": "故障工单 CLOSED，复盘文档已发出",
        "weight": 8,
    },
    {
        "tag": "cross_team",
        "work": "拉通 {team} 推进 {task}，对齐接口契约",
        "collab": "主持跨团队评审 1 场，达成排期共识",
        "output": "联调文档 1 份，会议纪要 1 份",
        "weight": 12,
    },
    {
        "tag": "mentor",
        "work": "辅导新人小 Z 串讲 {task} 业务背景，结对编程 1 小时",
        "collab": "Review 新人 PR 3 个，反馈建设性意见",
        "output": "新人首次独立提交 1 个中等复杂度任务",
        "weight": 8,
    },
    {
        "tag": "tech_debt",
        "work": "偿还 {task} 历史技术债，重构 legacy 模块",
        "collab": "与测试同学梳理回归用例 12 条",
        "output": "代码圈复杂度下降 35%，单测新增 8 条",
        "weight": 6,
    },
    {
        "tag": "incident",
        "work": "处理线上 P2 故障：{task} 接口 5xx 飙升",
        "collab": "拉应急群，配合客服安抚客户",
        "output": "故障恢复耗时 42 分钟，复盘时间线已整理",
        "weight": 5,
    },
    {
        "tag": "customer_complaint",
        "work": "处理客户工单：{task} 报表数据不一致",
        "collab": "与客户成功经理同步定位结论",
        "output": "客户回访满意，关闭 1 起投诉",
        "weight": 5,
    },
    {
        "tag": "promotion_defense",
        "work": "准备晋升答辩材料，整理 {task} 关键产出",
        "collab": "与 mentor 模拟答辩 1 次",
        "output": "答辩 PPT v2 完成，案例 3 个",
        "weight": 3,
    },
    {
        "tag": "doc_only",
        "work": "整理 {task} 设计文档与 ADR",
        "collab": "邀请架构师 review",
        "output": "技术方案文档 1 篇，ADR 2 篇",
        "weight": 5,
    },
    {
        "tag": "meeting_heavy",
        "work": "全天会议：周会、双周会、季度规划会",
        "collab": "向上汇报团队季度 OKR 进展",
        "output": "会议纪要 3 份，无代码产出（官僚日）",
        "weight": 4,
    },
    {
        "tag": "research",
        "work": "调研 {task} 业界方案，输出对比报告",
        "collab": "与算法同学对齐选型",
        "output": "调研报告 1 篇，给出 3 个备选方案",
        "weight": 4,
    },
    {
        "tag": "low_performance",
        "work": "处理 {task} 杂事，进度 {pct}%，存在卡点",
        "collab": "未主动同步，被动响应 1 次问询",
        "output": "无显著产出，待 owner 催促",
        "weight": 5,
    },
    {
        "tag": "innovation",
        "work": "尝试 {task} 工程提效小工具，PoC 跑通",
        "collab": "与平台组分享思路",
        "output": "内部小工具 1 个，节省人均 0.5h/天",
        "weight": 5,
    },
]

# 海外场景英文片段（用于跨国员工日报中英文混合）
EN_WORK_FRAGMENTS = [
    "Synced with {team} on {task} progress",
    "Reviewed PR #{pr} from offshore team",
    "Joined global standup, timezone overlap 3h",
    "Drafted RFC for {task} cross-region deployment",
    "Pair-debugged prod issue with US SRE",
]


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------
def _pick_name(idx: int, rng: random.Random) -> str:
    """生成中文姓名；偶数索引给男性名，奇数给女性名。"""
    surname = rng.choice(SURNAMES)
    if idx % 2 == 0:
        return surname + rng.choice(GIVEN_NAMES_M)
    return surname + rng.choice(GIVEN_NAMES_F)


def _weighted_pick(items: list[dict], rng: random.Random) -> dict:
    """按 weight 字段加权随机选一个场景模板。"""
    weights = [it["weight"] for it in items]
    return rng.choices(items, weights=weights, k=1)[0]


def _task_name(rng: random.Random) -> str:
    """随机生成任务名（带 JIRA 编号，看起来真实）。"""
    verbs = [
        "重构",
        "优化",
        "上线",
        "回归",
        "排查",
        "迁移",
        "评审",
        "设计",
        "实现",
        "复盘",
    ]
    objects = [
        "订单中心接口",
        "用户画像",
        "推荐召回",
        "支付链路",
        "风控规则",
        "数据看板",
        "活动页",
        "搜索建议词",
        "营销弹窗",
        "登录态",
        "IM 长连接",
        "上传组件",
        "报表导出",
        "权限中心",
        "定时任务",
    ]
    jira = f"JIRA-{rng.randint(2000, 9999)}"
    return f"{jira} {rng.choice(verbs)}{rng.choice(objects)}"


def _fill_template(tpl: str, rng: random.Random) -> str:
    """填充模板占位符 {task}/{pct}/{pr}/{cov}/{team}。"""
    return tpl.format(
        task=_task_name(rng),
        pct=rng.choice([10, 30, 50, 70, 80, 90, 100]),
        pr=rng.randint(1000, 9999),
        cov=rng.choice([65, 72, 78, 82, 85, 88, 91]),
        team=rng.choice(["算法组", "前端组", "测试组", "运维组", "数据组", "产品组"]),
    )


def _is_overseas(location: str) -> bool:
    return location in OVERSEAS_LOCATIONS


def _level_distribution(max_level: int, count: int, rng: random.Random) -> list[str]:
    """按金字塔形分布生成 count 个员工的职级。"""
    levels = LEVELS_BY_TIER[max_level]
    # P5/P6 占大头，越往上越少
    weights = []
    for lv in levels:
        if lv.startswith("P"):
            num = int(lv[1:])
            # 数字越大权重越小（金字塔）
            w = max(1, 12 - num)
        else:
            # M 系列管理者少量
            w = 2
        weights.append(w)
    return rng.choices(levels, weights=weights, k=count)


# ---------------------------------------------------------------------------
# 员工清单生成
# ---------------------------------------------------------------------------
def _generate_employees(scale: str, config: dict, rng: random.Random) -> list[dict]:
    """生成一个规模档位的员工清单（含复杂场景标记）。"""
    n = config["sampled_employees"]
    departments = config["departments"]
    locations = config["locations"]
    max_level = config["max_level"]
    features = config["features"]

    levels = _level_distribution(max_level, n, rng)

    employees: list[dict] = []
    # 先按职级排序，便于构造汇报关系：高职级在前
    level_order = {lv: i for i, lv in enumerate(LEVELS_BY_TIER[max_level][::-1])}
    indexed_levels = sorted(
        enumerate(levels), key=lambda x: level_order.get(x[1], 0), reverse=True
    )

    # 第一位作为公司最高层（无汇报对象）
    emp_id_pool = [f"E{10000 + i:05d}" for i in range(n)]

    for new_idx, (orig_idx, level) in enumerate(indexed_levels):
        eid = emp_id_pool[orig_idx]
        dept = rng.choice(departments)
        loc = rng.choice(locations)
        name = _pick_name(orig_idx, rng)
        # 海外员工姓名用英文名
        if _is_overseas(loc):
            name = rng.choice(EN_NAMES) + " " + rng.choice(SURNAMES)

        # 找汇报对象：职级高于自己的最近一位（已加入 employees）
        reports_to = None
        for superior in employees:
            if level_order.get(superior["level"], 0) > level_order.get(level, 0):
                reports_to = superior["employee_id"]
                # 优先同部门，找不到再跨部门
                if superior["department"] == dept:
                    break

        emp = {
            "employee_id": eid,
            "name": name,
            "department": dept,
            "level": level,
            "location": loc,
            "reports_to": reports_to,
        }
        employees.append(emp)

    # 重新按 employee_id 排序，输出更可读
    employees.sort(key=lambda e: e["employee_id"])

    # 注入复杂场景标记
    _inject_complex_scenarios(employees, config, rng)

    return employees


def _inject_complex_scenarios(
    employees: list[dict], config: dict, rng: random.Random
) -> None:
    """为部分员工打上复杂场景标记（双线汇报 / 借调 / 官僚层 / 361 强制分布）。"""
    features = config["features"]
    n = len(employees)
    if n == 0:
        return

    # 收集可作为虚线项目经理的 M 系列员工
    managers = [e for e in employees if e["level"].startswith("M")]

    # 1) 双线汇报：超大型档，约 12% 员工有虚线项目经理
    if "dual_line_reporting" in features and len(managers) >= 2:
        candidates = [e for e in employees if not e["level"].startswith("M")]
        rng.shuffle(candidates)
        for emp in candidates[: max(2, n // 8)]:
            # 虚线项目经理必须不是实线主管本人
            dotted = [m for m in managers if m["employee_id"] != emp["reports_to"]]
            if dotted:
                emp["dotted_line_manager"] = rng.choice(dotted)["employee_id"]
                # 预埋评估冲突：实线评 A，虚线评 B
                emp["dual_line_conflict"] = {
                    "solid_manager_grade": "A",
                    "dotted_manager_grade": "B",
                    "conflict_note": "实线主管看重长期贡献，虚线项目经理看重本季度交付，评分存在冲突",
                }

    # 2) 矩阵借调：大型/超大型档，约 8% 员工本季度借调到其他团队
    if "matrix_secondment" in features:
        candidates = [e for e in employees if not e["level"].startswith("M")]
        rng.shuffle(candidates)
        for emp in candidates[: max(2, n // 12)]:
            other_depts = [d for d in config["departments"] if d != emp["department"]]
            if other_depts:
                emp["home_department"] = emp["department"]
                emp["current_department"] = rng.choice(other_depts)
                emp["secondment_note"] = (
                    "本季度借调，编制保留在原部门，绩效由借调团队主导评定"
                )

    # 3) 官僚层：大型/超大型档，挑 1-2 个 M2/M3 中层管理者，标记为"自己不产出只做汇报"
    if "bureaucratic_layer" in features:
        mid_managers = [e for e in employees if e["level"] in ("M2", "M3")]
        for emp in mid_managers[:2]:
            emp["bureaucratic"] = True
            emp["bureaucratic_note"] = (
                "中层管理者日常以开会、汇报为主，无直接代码/产品产出"
            )

    # 4) 361 强制分布：超大型档，约 10% 员工被强制打 3.25（末位）
    if "force_361" in features:
        candidates = [
            e for e in employees if not e["level"].startswith(("M3", "M4", "M5"))
        ]
        rng.shuffle(candidates)
        for emp in candidates[: max(3, n // 10)]:
            emp["forced_ranking"] = "3.25"
            emp["ranking_note"] = "361 强制分布末位，本季度绩效 3.25，需进入改进计划"


# ---------------------------------------------------------------------------
# 日报生成
# ---------------------------------------------------------------------------
def _build_daily_entry(emp: dict, week: int, day: int, rng: random.Random) -> dict:
    """生成单条日报（含工作内容/协作记录/产出物），按员工特征选场景。"""
    is_overseas = _is_overseas(emp["location"])
    is_bureaucratic = emp.get("bureaucratic", False)

    if is_bureaucratic:
        # 官僚日：只有会议和汇报
        tpl = SCENARIO_TEMPLATES[9]  # meeting_heavy
    else:
        tpl = _weighted_pick(SCENARIO_TEMPLATES, rng)

    work = _fill_template(tpl["work"], rng)
    collab = _fill_template(tpl["collab"], rng)
    output = _fill_template(tpl["output"], rng)

    # 海外员工日报中英文混合（约 50% 内容混入英文片段）
    if is_overseas and rng.random() < 0.5:
        en_frag = rng.choice(EN_WORK_FRAGMENTS).format(
            team=rng.choice(["algo team", "frontend team", "QA", "SRE", "data team"]),
            task=_task_name(rng),
            pr=rng.randint(1000, 9999),
        )
        work = f"{work}. {en_frag}."

    # 双线汇报员工：周五日报额外记录虚线项目经理对齐
    dotted = emp.get("dotted_line_manager")
    if dotted and day == 5:
        collab += f"；与虚线项目经理 {dotted} 周度对齐，存在交付节奏分歧"

    # 借调员工：日报部门标签使用借调后的部门
    dept_tag = emp.get("current_department") or emp["department"]

    return {
        "employee_id": emp["employee_id"],
        "name": emp["name"],
        "department": dept_tag,
        "location": emp["location"],
        "week": week,
        "day": day,
        "scenario_tag": tpl["tag"],
        "work_content": work,
        "collaboration": collab,
        "output": output,
    }


def _build_weekly_report(
    employees: list[dict], week: int, rng: random.Random
) -> list[dict]:
    """生成某一周的日报集合：每员工 5 个工作日。"""
    records: list[dict] = []
    for emp in employees:
        daily_entries = [
            _build_daily_entry(emp, week, day, rng)
            for day in range(1, WORKDAYS_PER_WEEK + 1)
        ]
        records.append(
            {
                "employee_id": emp["employee_id"],
                "name": emp["name"],
                "department": emp.get("current_department") or emp["department"],
                "week": week,
                "daily_reports": daily_entries,
            }
        )
    return records


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def generate_for_scale(scale: str, output_dir: Path, seed: int = 42) -> dict:
    """生成单个规模档位的全部数据，返回统计信息。"""
    config = SCALE_CONFIGS[scale]
    # 用 crc32 做稳定哈希，避免 Python 字符串 hash 随机化导致种子不可复现
    rng = random.Random(seed + zlib.crc32(scale.encode("utf-8")))

    employees = _generate_employees(scale, config, rng)
    out_dir = output_dir / scale
    _write_json(
        out_dir / "employees.json",
        {
            "scale": scale,
            "label": config["label"],
            "total_employees_claimed": config["total_employees"],
            "sampled_employees": len(employees),
            "features": config["features"],
            "employees": employees,
        },
    )

    daily_total = 0
    for week in range(1, WEEKS + 1):
        records = _build_weekly_report(employees, week, rng)
        daily_total += sum(len(r["daily_reports"]) for r in records)
        _write_json(
            out_dir / f"weekly_reports_week{week}.json",
            {
                "scale": scale,
                "week": week,
                "records": records,
            },
        )

    return {
        "scale": scale,
        "label": config["label"],
        "employees_in_file": len(employees),
        "weekly_records": len(employees) * WEEKS,
        "daily_reports": daily_total,
        "features": config["features"],
    }


def generate_all(output_dir: Path, seed: int = 42) -> list[dict]:
    """生成全部 5 个规模档位数据。"""
    summary = []
    for scale in SCALE_CONFIGS:
        stat = generate_for_scale(scale, output_dir, seed=seed)
        summary.append(stat)
        print(
            f"[{stat['label']:<6}] {scale:<8} "
            f"员工 {stat['employees_in_file']:>4} 人 "
            f"周报 {stat['weekly_records']:>5} 条 "
            f"日报 {stat['daily_reports']:>6} 条 "
            f"场景 {','.join(stat['features']) or '-'}"
        )
    _write_json(output_dir / "_summary.json", {"scales": summary})
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentValue-AI 多规模公司试点数据生成器")
    parser.add_argument(
        "--scale",
        required=True,
        choices=list(SCALE_CONFIGS.keys()) + ["all"],
        help="规模档位：startup/growth/medium/large/huge 或 all",
    )
    parser.add_argument(
        "--output",
        default="data/pilot/",
        help="输出根目录，默认 data/pilot/",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="随机种子，默认 42（同种子可复现）",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output)
    if args.scale == "all":
        generate_all(output_dir, seed=args.seed)
    else:
        stat = generate_for_scale(args.scale, output_dir, seed=args.seed)
        print(
            f"[{stat['label']}] {stat['scale']} 完成："
            f"员工 {stat['employees_in_file']} 人 / "
            f"周报 {stat['weekly_records']} 条 / "
            f"日报 {stat['daily_reports']} 条"
        )
    print(f"数据已写入: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
