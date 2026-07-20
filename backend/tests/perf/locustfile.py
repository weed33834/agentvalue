"""
AgentValue-AI 性能测试基线（Locust）

启动方式（确保后端已在 http://localhost:8000 运行）：
    locust -f tests/perf/locustfile.py --host http://localhost:8000

随后在浏览器打开 http://localhost:8089 配置并发用户数与爬升速率后开始压测。
未安装 locust 时可先执行：pip install locust（locust 为可选开发依赖，不纳入 requirements.txt）。

测试场景：AgentValueUser 模拟员工的完整用户流程：
    1. 提交日报输入            POST /api/v1/inputs
    2. 触发异步评估            POST /api/v1/evaluations
    3. 轮询评估任务结果        GET  /api/v1/evaluations/jobs/{job_id}
    4. 查看个人 dashboard      GET  /api/v1/employees/{id}/dashboard
"""

import random
import uuid

from locust import HttpUser, between, task


class AgentValueUser(HttpUser):
    """模拟员工/主管用户行为，覆盖从输入到 dashboard 的完整流程。

    继承 locust.HttpUser（HttpUser 本身继承自 locust.User），
    self.client 即 locust.clients.HttpSession，直接用于发请求。
    所有请求统一带 x-user-id / x-user-role 头，适配演示模式鉴权。
    """

    # 任务间隔：1~3 秒，模拟真实用户思考与操作停顿
    wait_time = between(1, 3)
    weight = 1

    def on_start(self):
        """每个虚拟用户启动时初始化身份与请求头。

        采用演示模式（x-user-id / x-user-role），不依赖 JWT 登录，
        便于在无鉴权基础设施时直接跑通基线压测。
        """
        self.role = random.choice(["employee", "manager", "hr", "admin"])
        self.user_id = f"E{random.randint(1000, 9999)}"
        self.headers = {
            "x-user-id": self.user_id,
            "x-user-role": self.role,
        }
        # 触发评估后缓存的最近一次 job_id，供轮询任务复用
        self.last_job_id = None
        self.period = "2026-W26"

    @task(3)
    def submit_input(self):
        """提交日报输入（POST /api/v1/inputs）"""
        payload = {
            "employee_id": self.user_id,
            "period": self.period,
            "type": "daily_report",
            "content": f"性能测试日报 {uuid.uuid4().hex[:8]}：完成模块开发并修复 2 个 Bug",
        }
        self.client.post(
            "/api/v1/inputs",
            json=payload,
            headers=self.headers,
            name="/inputs",
        )

    @task(2)
    def trigger_evaluation(self):
        """触发异步评估（POST /api/v1/evaluations），缓存 job_id 供轮询"""
        payload = {
            "employee_id": self.user_id,
            "period": self.period,
            "raw_inputs": [
                {
                    "input_id": f"perf-{uuid.uuid4().hex[:8]}",
                    "type": "daily_report",
                    "content": "性能测试：主导完成核心模块重构，性能提升 40%",
                }
            ],
        }
        resp = self.client.post(
            "/api/v1/evaluations",
            json=payload,
            headers=self.headers,
            name="/evaluations",
        )
        if resp.status_code == 200:
            job_id = resp.json().get("job_id")
            if job_id:
                self.last_job_id = job_id

    @task(2)
    def poll_evaluation_job(self):
        """轮询评估任务结果（GET /api/v1/evaluations/jobs/{job_id}）。

        复用最近一次触发的 job_id；若无则跳过该轮。
        """
        job_id = self.last_job_id
        if not job_id:
            return
        self.client.get(
            f"/api/v1/evaluations/jobs/{job_id}",
            headers=self.headers,
            name="/evaluations/jobs/[job_id]",
        )

    @task(1)
    def get_dashboard(self):
        """查看个人 dashboard（GET /api/v1/employees/{id}/dashboard）"""
        self.client.get(
            f"/api/v1/employees/{self.user_id}/dashboard",
            headers=self.headers,
            name="/employees/[id]/dashboard",
        )
