"""
scripts/run_mock_evaluations.py 单元测试
直接调用 main() 函数，验证输出格式与异常处理路径。

main() 内部默认构造云端 ModelRouter，测试中替换为 Mock Provider 路由，
避免依赖真实 API Key；异常分支通过注入可控的图替身验证。
"""

import pytest

from data.loader import ProfileLoader


def _profile_count():
    """当前画像数据中的画像数量"""
    return len(ProfileLoader().list_profiles())


class _FakeGraph:
    """可控的评估图替身，用于验证 main() 的分支逻辑"""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    async def ainvoke(self, state):
        if self._exc:
            raise self._exc
        return self._result or {}


def _patch_mock_router(monkeypatch):
    """将 run_mock_evaluations.ModelRouter 替换为返回 Mock Provider 的路由"""
    from scripts import run_mock_evaluations
    from eval.evaluate import build_mock_model_router

    monkeypatch.setattr(
        run_mock_evaluations,
        "ModelRouter",
        lambda settings: build_mock_model_router("L0"),
    )


class TestRunMockMain:
    """run_mock_evaluations.main() 直接调用"""

    @pytest.mark.asyncio
    async def test_main_produces_output_for_all_profiles(self, monkeypatch, capsys):
        """main() 应为每个画像输出评估结果，且包含关键格式字段"""
        from scripts import run_mock_evaluations

        _patch_mock_router(monkeypatch)

        await run_mock_evaluations.main()
        out = capsys.readouterr().out

        expected_count = _profile_count()
        assert expected_count > 0
        # 每个画像都会打印员工信息与关键字段
        assert out.count("员工:") == expected_count
        assert out.count("状态:") == expected_count
        assert out.count("综合得分:") == expected_count
        assert out.count("模型档位:") == expected_count

    @pytest.mark.asyncio
    async def test_main_output_format_contains_expected_headers(
        self, monkeypatch, capsys
    ):
        """输出应包含标准格式的表头与字段标签"""
        from scripts import run_mock_evaluations

        _patch_mock_router(monkeypatch)

        await run_mock_evaluations.main()
        out = capsys.readouterr().out

        # 周期标签与员工视图总结标签
        assert "周期:" in out
        assert "员工视图总结:" in out
        # 分隔线（60 个等号）
        assert "=" * 60 in out

    @pytest.mark.asyncio
    async def test_main_handles_graph_exception(self, monkeypatch, capsys):
        """graph.ainvoke 抛异常时 main() 应打印 [EXCEPTION] 而非崩溃"""
        from scripts import run_mock_evaluations

        def fake_create_graph(*args, **kwargs):
            return _FakeGraph(exc=RuntimeError("boom"))

        monkeypatch.setattr(
            run_mock_evaluations, "create_evaluation_graph", fake_create_graph
        )

        # 不应抛出异常
        await run_mock_evaluations.main()
        out = capsys.readouterr().out

        expected_count = _profile_count()
        assert out.count("[EXCEPTION]") == expected_count
        assert "boom" in out
        # 异常路径不应打印综合得分
        assert "综合得分:" not in out

    @pytest.mark.asyncio
    async def test_main_handles_result_error(self, monkeypatch, capsys):
        """结果包含 error 字段时 main() 应打印 [ERROR] 并跳过该画像"""
        from scripts import run_mock_evaluations

        def fake_create_graph(*args, **kwargs):
            return _FakeGraph(result={"error": "评估失败: 输入不足"})

        monkeypatch.setattr(
            run_mock_evaluations, "create_evaluation_graph", fake_create_graph
        )

        await run_mock_evaluations.main()
        out = capsys.readouterr().out

        expected_count = _profile_count()
        assert out.count("[ERROR]") == expected_count
        assert "评估失败: 输入不足" in out
        # 出错路径不应打印综合得分
        assert "综合得分:" not in out

    @pytest.mark.asyncio
    async def test_main_no_profiles_completes_silently(self, monkeypatch, capsys):
        """无画像数据时 main() 应正常结束，不输出任何画像结果"""
        from scripts import run_mock_evaluations

        class EmptyLoader:
            def list_profiles(self):
                return []

        monkeypatch.setattr(run_mock_evaluations, "ProfileLoader", EmptyLoader)

        # 不应抛出异常
        await run_mock_evaluations.main()
        out = capsys.readouterr().out

        assert "员工:" not in out
        assert "综合得分:" not in out
        assert "[ERROR]" not in out
        assert "[EXCEPTION]" not in out
