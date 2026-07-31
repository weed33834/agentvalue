#!/usr/bin/env python3
"""AgentValue 全链路端到端冒烟测试 (E2E Smoke Test)

针对**运行中的真实服务**验证从登录到出结果的完整因果链路，而非单元测试的
mock 路径。覆盖 4 个角色、评估主流程、HITL 审批、异常与权限边界。

用法::

    python scripts/e2e_smoke.py                        # 默认 http://127.0.0.1:8000
    python scripts/e2e_smoke.py --base-url http://host:8000
    python scripts/e2e_smoke.py --json report.json     # 导出机器可读报告

退出码：0 = 全部通过；1 = 存在失败用例。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEMO_PASSWORD = "agentvalue123"

ACCOUNTS = {
    "employee": "employee@agentvalue.ai",
    "manager": "manager@agentvalue.ai",
    "hr": "hr@agentvalue.ai",
    "admin": "admin@agentvalue.ai",
}


@dataclass
class Case:
    name: str
    group: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class Report:
    cases: list[Case] = field(default_factory=list)

    def add(self, name: str, group: str, passed: bool, detail: str = "", duration_ms: int = 0) -> None:
        self.cases.append(Case(name, group, passed, detail, duration_ms))
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if not c.passed]

    def summary(self) -> str:
        total = len(self.cases)
        ok = total - len(self.failed)
        return f"{ok}/{total} passed"


class E2ERunner:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self.tokens: dict[str, str] = {}
        self.report = Report()

    # ---------------------------------------------------------------- utils
    def _hdr(self, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def _post_eval_payload(self, role: str = "manager", employee_id: str = "E1001", period: str = "2026-Q3"):
        """主链路评估创建请求（抽出以便重试逻辑复用）。"""
        return self.client.post(
            f"{self.base}/api/v1/evaluations",
            headers=self._hdr(role),
            json={
                "employee_id": employee_id,
                "period": period,
                "raw_inputs": [
                    {
                        "type": "self_report",
                        "content": "本季度主导支付网关重构，QPS 从 800 提升到 3200，"
                        "P99 延迟由 420ms 降至 95ms；牵头 3 次跨部门技术评审；"
                        "指导 2 名新人完成上手。",
                    },
                    {
                        "type": "peer_feedback",
                        "content": "技术方案严谨，沟通主动，线上问题响应及时。",
                    },
                ],
            },
        )

    def check(self, name: str, group: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        t0 = time.time()
        try:
            passed, detail = fn()
        except Exception as exc:  # noqa: BLE001 - 冒烟测试需捕获一切
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        self.report.add(name, group, passed, detail, int((time.time() - t0) * 1000))
        return passed

    # ------------------------------------------------------------- 1. 基础设施
    def phase_infra(self) -> None:
        print("\n[1/7] 基础设施 & 健康检查")

        def _health() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/health")
            return r.status_code == 200, f"status={r.status_code}"

        def _ready() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/readyz")
            body = r.json()
            checks = body.get("checks", {})
            # db 必须为 true；provider 在未配置 LLM key 时允许 false
            return bool(checks.get("db")), f"db={checks.get('db')} redis={checks.get('redis')} provider={checks.get('provider')}"

        def _live() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/livez")
            return r.status_code == 200, f"status={r.status_code}"

        def _openapi() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/openapi.json")
            spec = r.json()
            n = sum(len([m for m in ops if m in ("get", "post", "put", "patch", "delete")]) for ops in spec["paths"].values())
            return n > 100, f"{len(spec['paths'])} paths / {n} operations"

        def _metrics() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/metrics")
            return r.status_code == 200 and b"python_info" in r.content or r.status_code == 200, f"status={r.status_code} bytes={len(r.content)}"

        self.check("GET /health 返回 200", "infra", _health)
        self.check("GET /livez 返回 200", "infra", _live)
        self.check("GET /readyz 数据库连通", "infra", _ready)
        self.check("OpenAPI schema 可生成且路由完整", "infra", _openapi)
        self.check("Prometheus /metrics 暴露", "infra", _metrics)

    # --------------------------------------------------------------- 2. 认证
    def phase_auth(self) -> None:
        print("\n[2/7] 认证 & 4 角色登录")

        self.client.post(f"{self.base}/api/v1/auth/seed-demo-users")

        for role, email in ACCOUNTS.items():
            def _login(email=email, role=role) -> tuple[bool, str]:
                r = self.client.post(
                    f"{self.base}/api/v1/auth/login",
                    json={"email": email, "password": DEMO_PASSWORD},
                )
                if r.status_code != 200:
                    return False, f"status={r.status_code} body={r.text[:120]}"
                data = r.json()
                self.tokens[role] = data["access_token"]
                return data.get("role") == role, f"role={data.get('role')} user_id={data.get('user_id')}"

            self.check(f"{role} 登录并签发 JWT", "auth", _login)

        def _me() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/auth/me", headers=self._hdr("manager"))
            return r.status_code == 200, f"status={r.status_code} name={r.json().get('name') if r.status_code==200 else ''}"

        def _bad_pwd() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/auth/login",
                json={"email": ACCOUNTS["manager"], "password": "wrong-password"},
            )
            return r.status_code in (400, 401), f"status={r.status_code}（应为 401）"

        def _no_token() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/auth/me")
            # 生产配置(AUTH_DEMO_MODE=false)下应为 401。
            # 演示模式下匿名请求会回落为 user_id="anonymous" 的 employee，
            # 查库无此人 → 404。两者都表示"拿不到任何真实用户数据"，均可接受；
            # 关键是绝不能返回 200 泄露他人信息。生产环境由 Settings validator
            # 强制 auth_demo_mode=false，故不存在匿名兜底路径。
            return r.status_code in (401, 403, 404), f"status={r.status_code}（未返回 200 即安全）"

        def _bad_token() -> tuple[bool, str]:
            r = self.client.get(
                f"{self.base}/api/v1/auth/me",
                headers={"Authorization": "Bearer not.a.real.jwt"},
            )
            return r.status_code in (401, 403), f"status={r.status_code}（应拒绝）"

        def _refresh() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/auth/refresh",
                headers=self._hdr("manager"),
            )
            return r.status_code in (200, 401, 422), f"status={r.status_code}"

        self.check("GET /auth/me 返回当前用户", "auth", _me)
        self.check("错误密码被拒绝", "auth-negative", _bad_pwd)
        self.check("缺失 Token 被拒绝", "auth-negative", _no_token)
        self.check("伪造 Token 被拒绝", "auth-negative", _bad_token)
        self.check("Token 刷新接口可达", "auth", _refresh)

    # ----------------------------------------------------------- 3. 评估主链路
    def phase_evaluation(self) -> Optional[str]:
        print("\n[3/7] 评估主链路（创建 → 异步执行 → 落库 → 读取）")
        eval_id: Optional[str] = None
        job_id: Optional[str] = None

        def _create() -> tuple[bool, str]:
            nonlocal job_id
            # POST /evaluations 限流 10/min；主链路用例必须跑通，遇 429 退避重试。
            for attempt in range(4):
                r = self._post_eval_payload()
                if r.status_code != 429:
                    break
                time.sleep(20)
            if r.status_code not in (200, 201, 202):
                return False, f"status={r.status_code} body={r.text[:200]}"
            data = r.json()
            job_id = data.get("job_id") or data.get("id")
            return bool(job_id), f"job_id={job_id} status={data.get('status')}"

        ok = self.check("POST /evaluations 创建评估任务", "evaluation", _create)
        if not ok or not job_id:
            return None

        def _poll() -> tuple[bool, str]:
            nonlocal eval_id
            deadline = time.time() + 90
            last = ""
            while time.time() < deadline:
                r = self.client.get(f"{self.base}/api/v1/evaluations/jobs/{job_id}", headers=self._hdr("manager"))
                if r.status_code != 200:
                    return False, f"轮询 status={r.status_code} body={r.text[:150]}"
                data = r.json()
                st = data.get("status")
                last = st or ""
                if st in ("succeeded", "success", "completed", "done"):
                    eval_id = data.get("evaluation_id") or (data.get("result") or {}).get("evaluation_id")
                    return bool(eval_id), f"status={st} evaluation_id={eval_id}"
                if st in ("failed", "error"):
                    return False, f"任务失败: {str(data.get('error'))[:200]}"
                time.sleep(2)
            return False, f"90s 内未完成，最后状态={last}"

        ok = self.check("异步任务在 90s 内完成并产出 evaluation_id", "evaluation", _poll)
        if not ok or not eval_id:
            return None

        def _get() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/{eval_id}", headers=self._hdr("manager"))
            if r.status_code != 200:
                return False, f"status={r.status_code}"
            d = r.json()
            # 契约字段名以 Evaluation 模型为准：overall_score（total_score/score 为历史别名，容错保留）
            score = d.get("overall_score")
            if score is None:
                score = d.get("total_score", d.get("score"))
            if score is None:
                return False, f"评分缺失 status={d.get('status')}"
            if not 0 <= float(score) <= 100:
                return False, f"评分越界 score={score}"
            # AI 起草完成后应进入 ai_drafted（待主管复核），而非停留在 pending/failed
            ok_status = d.get("status") in ("ai_drafted", "under_review", "approved")
            return ok_status, f"score={score} status={d.get('status')}"

        def _manager_view() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/{eval_id}/manager-view", headers=self._hdr("manager"))
            return r.status_code == 200, f"status={r.status_code}"

        def _employee_view() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/{eval_id}/employee-view", headers=self._hdr("employee"))
            return r.status_code == 200, f"status={r.status_code}"

        def _evidence() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/{eval_id}/evidence", headers=self._hdr("manager"))
            return r.status_code == 200, f"status={r.status_code}"

        def _audit() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/{eval_id}/audit-logs", headers=self._hdr("hr"))
            return r.status_code == 200, f"status={r.status_code}"

        def _not_found() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/does-not-exist-xyz", headers=self._hdr("manager"))
            return r.status_code == 404, f"status={r.status_code}（应为 404）"

        def _job_no_leak() -> tuple[bool, str]:
            """回归：任务状态接口不得向员工泄露 manager_view（双视图隔离）。"""
            r = self.client.get(f"{self.base}/api/v1/evaluations/jobs/{job_id}", headers=self._hdr("employee"))
            if r.status_code in (403, 404):
                return True, f"status={r.status_code}（员工无权访问该任务）"
            if r.status_code != 200:
                return False, f"status={r.status_code}"
            ev = (r.json() or {}).get("evaluation") or {}
            leaked = [k for k in ("manager_view", "audit") if k in ev]
            return not leaked, f"泄露字段={leaked or '无'}"

        def _job_has_eval_id() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/evaluations/jobs/{job_id}", headers=self._hdr("manager"))
            return bool((r.json() or {}).get("evaluation_id")), "任务载荷顶层含 evaluation_id"

        self.check("GET /evaluations/{id} 结果已落库且含评分", "evaluation", _get)
        self.check("任务状态不向员工泄露 manager_view", "security", _job_no_leak)
        self.check("任务状态顶层暴露 evaluation_id", "evaluation", _job_has_eval_id)
        self.check("主管视图可访问", "evaluation", _manager_view)
        self.check("员工视图可访问", "evaluation", _employee_view)
        self.check("证据链可追溯", "evaluation", _evidence)
        self.check("审计日志可查询", "evaluation", _audit)
        self.check("不存在的评估返回 404", "evaluation-negative", _not_found)
        return eval_id

    # ----------------------------------------------------- 4. 输入校验与异常
    def phase_validation(self) -> None:
        print("\n[4/7] 输入校验 & 异常处理")

        def _missing_field() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("manager"),
                json={"period": "2026-Q3"},
            )
            return r.status_code == 422, f"status={r.status_code}（应为 422）"

        def _empty_inputs() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("manager"),
                json={"employee_id": "E1001", "period": "2026-Q3", "raw_inputs": []},
            )
            return r.status_code in (200, 201, 202, 400, 422), f"status={r.status_code}（不应 5xx）"

        def _oversized() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("manager"),
                json={
                    "employee_id": "E1001",
                    "period": "2026-Q3",
                    "raw_inputs": [{"type": "self_report", "content": "超长文本" * 50000}],
                },
            )
            return r.status_code < 500, f"status={r.status_code}（超长输入不应 5xx）"

        def _injection() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("manager"),
                json={
                    "employee_id": "E1001'; DROP TABLE users;--",
                    "period": "2026-Q3",
                    "raw_inputs": [{"type": "self_report", "content": "test"}],
                },
            )
            if r.status_code >= 500:
                return False, f"status={r.status_code}（SQL 注入串导致 5xx）"
            # 确认 users 表仍在
            probe = self.client.get(f"{self.base}/api/v1/auth/me", headers=self._hdr("manager"))
            return probe.status_code == 200, f"status={r.status_code}，注入后 users 表仍可用={probe.status_code==200}"

        def _malformed_json() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers={**self._hdr("manager"), "Content-Type": "application/json"},
                content=b"{not-valid-json",
            )
            return r.status_code in (400, 422), f"status={r.status_code}（应为 400/422）"

        def _wrong_method() -> tuple[bool, str]:
            r = self.client.delete(f"{self.base}/api/v1/evaluations", headers=self._hdr("manager"))
            return r.status_code in (404, 405), f"status={r.status_code}（应为 405）"

        self.check("缺失必填字段返回 422", "validation", _missing_field)
        self.check("空输入数组被优雅处理", "validation", _empty_inputs)
        self.check("超长输入不导致 5xx", "validation", _oversized)
        self.check("SQL 注入串被安全处理", "security", _injection)
        self.check("畸形 JSON 返回 400/422", "validation", _malformed_json)
        self.check("错误 HTTP 方法返回 405", "validation", _wrong_method)

    # --------------------------------------------------------- 5. RBAC 权限边界
    def phase_rbac(self) -> None:
        print("\n[5/7] RBAC 权限边界")

        def _employee_cannot_admin() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/admin/users", headers=self._hdr("employee"))
            return r.status_code in (401, 403), f"status={r.status_code}（员工访问管理端应被拒）"

        def _manager_cannot_admin() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/admin/users", headers=self._hdr("manager"))
            return r.status_code in (401, 403), f"status={r.status_code}（主管访问管理端应被拒）"

        def _admin_can_admin() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/api/v1/admin/users", headers=self._hdr("admin"))
            return r.status_code == 200, f"status={r.status_code}（管理员应可访问）"

        def _employee_cannot_create_eval() -> tuple[bool, str]:
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("employee"),
                json={"employee_id": "E1002", "period": "2026-Q3", "raw_inputs": [{"type": "self_report", "content": "x"}]},
            )
            return r.status_code in (401, 403, 429), f"status={r.status_code}（员工不应能为他人发起评估）"

        def _manager_cross_team_blocked() -> tuple[bool, str]:
            """回归：主管不得为非直属下属发起评估（写接口需与读接口权限一致）。

            E1002 的 manager_id = M002，用 M001（manager）发起应被拒。
            """
            r = self.client.post(
                f"{self.base}/api/v1/evaluations",
                headers=self._hdr("manager"),
                json={
                    "employee_id": "E1002",
                    "period": "2026-Q3",
                    "raw_inputs": [{"type": "self_report", "content": "跨团队越权探测"}],
                },
            )
            return r.status_code in (401, 403, 429), f"status={r.status_code}（跨团队写入应被拒）"

        def _header_spoof() -> tuple[bool, str]:
            """演示模式关闭时，x-user-role 头不应能提权。"""
            r = self.client.get(
                f"{self.base}/api/v1/admin/users",
                headers={**self._hdr("employee"), "x-user-role": "admin"},
            )
            return r.status_code in (401, 403), f"status={r.status_code}（Header 提权应无效）"

        self.check("员工被拒绝访问管理端", "rbac", _employee_cannot_admin)
        self.check("主管被拒绝访问管理端", "rbac", _manager_cannot_admin)
        self.check("管理员可访问管理端", "rbac", _admin_can_admin)
        self.check("员工不能为他人发起评估", "rbac", _employee_cannot_create_eval)
        self.check("主管不能为非直属下属发起评估", "rbac", _manager_cross_team_blocked)
        self.check("x-user-role 头无法越权提权", "security", _header_spoof)

    # ------------------------------------------------------- 6. HITL 人机协同
    def phase_hitl(self, eval_id: Optional[str]) -> None:
        print("\n[6/7] HITL 人在回路（中断 / 恢复 / 审批）")
        thread_id: Optional[str] = None

        def _start_interrupt() -> tuple[bool, str]:
            nonlocal thread_id
            r = self.client.post(
                f"{self.base}/api/v1/evaluations-interrupt",
                headers=self._hdr("manager"),
                json={
                    "employee_id": "E1001",
                    "period": "2026-Q4",
                    "raw_inputs": [{"type": "self_report", "content": "季度交付 3 个核心模块，代码评审通过率 98%。"}],
                },
            )
            if r.status_code not in (200, 201, 202):
                return False, f"status={r.status_code} body={r.text[:180]}"
            d = r.json()
            thread_id = d.get("thread_id")
            return bool(thread_id), f"thread_id={thread_id} status={d.get('status')}"

        ok = self.check("发起可中断评估（HITL）", "hitl", _start_interrupt)

        if ok and thread_id:
            def _state() -> tuple[bool, str]:
                r = self.client.get(
                    f"{self.base}/api/v1/evaluations-interrupt/{thread_id}/state",
                    headers=self._hdr("manager"),
                )
                return r.status_code == 200, f"status={r.status_code} body={r.text[:150]}"

            def _resume() -> tuple[bool, str]:
                r = self.client.post(
                    f"{self.base}/api/v1/evaluations-interrupt/{thread_id}/resume",
                    headers=self._hdr("manager"),
                    json={"action": "approve", "comment": "E2E 自动化审批通过"},
                )
                return r.status_code in (200, 202, 400, 409), f"status={r.status_code}"

            self.check("查询中断状态（图状态可读）", "hitl", _state)
            self.check("恢复执行（resume）", "hitl", _resume)

            def _state_after_resume() -> tuple[bool, str]:
                """resume 后线程应已收敛（不再是 awaiting_review 的活跃中断）。

                返回 404 表示线程已完成并被清理，属于预期终态；
                返回 200 则其状态不应再是 awaiting_review。
                """
                r = self.client.get(
                    f"{self.base}/api/v1/evaluations-interrupt/{thread_id}/state",
                    headers=self._hdr("manager"),
                )
                if r.status_code == 404:
                    return True, "线程已完成并清理（终态）"
                if r.status_code == 200:
                    st = str(r.json())
                    return "awaiting_review" not in st, f"status=200 已离开待审状态={'awaiting_review' not in st}"
                return False, f"status={r.status_code}"

            self.check("resume 后中断线程收敛到终态", "hitl", _state_after_resume)

        if eval_id:
            def _approve() -> tuple[bool, str]:
                r = self.client.post(
                    f"{self.base}/api/v1/evaluations/{eval_id}/approve",
                    headers=self._hdr("manager"),
                    json={"comment": "E2E 审批"},
                )
                return r.status_code in (200, 202, 400, 409), f"status={r.status_code}"

            def _appeal() -> tuple[bool, str]:
                r = self.client.post(
                    f"{self.base}/api/v1/evaluations/{eval_id}/appeal",
                    headers=self._hdr("employee"),
                    json={"reason": "E2E 申诉：希望复核第三项指标"},
                )
                return r.status_code in (200, 201, 202, 400, 409), f"status={r.status_code}"

            def _feedback() -> tuple[bool, str]:
                r = self.client.post(
                    f"{self.base}/api/v1/evaluations/{eval_id}/feedback",
                    headers=self._hdr("employee"),
                    json={"rating": 4, "comment": "E2E 反馈"},
                )
                return r.status_code in (200, 201, 202, 400, 422), f"status={r.status_code}"

            self.check("主管审批通过", "hitl", _approve)
            self.check("员工发起申诉", "hitl", _appeal)
            self.check("员工提交反馈", "hitl", _feedback)

    # ------------------------------------------------------- 7. 并发与稳定性
    def phase_resilience(self) -> None:
        print("\n[7/7] 并发 & 稳定性")

        def _concurrent_reads() -> tuple[bool, str]:
            import concurrent.futures

            def one(_):
                c = httpx.Client(timeout=30)
                try:
                    return c.get(f"{self.base}/api/v1/auth/me", headers=self._hdr("manager")).status_code
                finally:
                    c.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                codes = list(ex.map(one, range(40)))
            ok = all(c == 200 for c in codes)
            return ok, f"40 并发请求，200 数={codes.count(200)}/40"

        def _concurrent_writes() -> tuple[bool, str]:
            import concurrent.futures

            def one(i):
                c = httpx.Client(timeout=60)
                try:
                    r = c.post(
                        f"{self.base}/api/v1/evaluations",
                        headers=self._hdr("manager"),
                        json={
                            "employee_id": "E1001",
                            "period": f"2026-CC{i}",
                            "raw_inputs": [{"type": "self_report", "content": f"并发压测样本 {i}"}],
                        },
                    )
                    return r.status_code
                finally:
                    c.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                codes = list(ex.map(one, range(8)))
            no5xx = all(c < 500 for c in codes)
            return no5xx, f"8 并发写入，状态码={sorted(set(codes))}，无 5xx={no5xx}"

        def _rate_limit_behavior() -> tuple[bool, str]:
            codes = []
            for _ in range(30):
                codes.append(self.client.get(f"{self.base}/health").status_code)
            return all(c in (200, 429) for c in codes), f"30 次快速请求，状态码集合={sorted(set(codes))}"

        def _still_healthy() -> tuple[bool, str]:
            r = self.client.get(f"{self.base}/health")
            return r.status_code == 200, f"压测后 health={r.status_code}"

        self.check("40 并发读请求全部成功", "resilience", _concurrent_reads)
        self.check("8 并发写请求无 5xx", "resilience", _concurrent_writes)
        self.check("高频请求限流行为正常", "resilience", _rate_limit_behavior)
        self.check("压测后服务仍健康", "resilience", _still_healthy)

    # ------------------------------------------------------------------ run
    def run(self) -> Report:
        self.phase_infra()
        self.phase_auth()
        eval_id = self.phase_evaluation()
        self.phase_validation()
        self.phase_rbac()
        self.phase_hitl(eval_id)
        self.phase_resilience()
        return self.report


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentValue 全链路 E2E 冒烟测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--json", dest="json_out", help="导出 JSON 报告路径")
    args = parser.parse_args()

    print("=" * 72)
    print(f"AgentValue E2E Smoke Test → {args.base_url}")
    print("=" * 72)

    runner = E2ERunner(args.base_url)
    report = runner.run()

    print("\n" + "=" * 72)
    print(f"结果: {report.summary()}")
    if report.failed:
        print(f"\n失败用例 ({len(report.failed)}):")
        for c in report.failed:
            print(f"  - [{c.group}] {c.name}: {c.detail}")
    print("=" * 72)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "base_url": args.base_url,
                    "summary": report.summary(),
                    "cases": [asdict(c) for c in report.cases],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"JSON 报告已写入 {args.json_out}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
