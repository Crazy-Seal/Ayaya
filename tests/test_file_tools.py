from tests.model_capture import image_urls, image_kind, encoded_tool
from app.agent.models.openai_message_names import TOOL_IMAGE_MESSAGE_NAME
import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.agent.tools.read_file import read_file
from app.agent.tools.read_image import read_image
from app.agent.tools.apply_patch import apply_patch
from app.agent.tools.results.artifacts import ToolResultArtifactStore
from app.agent.tools.results.normalizer import ToolResultNormalizer
from app.runtime import get_tool_results_dir
from app.agent.tools.core.registry import ToolRegistry


def _context(session_id: str = "session-a") -> SimpleNamespace:
    """为桌面或文件工具构造当前用例的运行上下文。

    Args:
        session_id (str): 当前操作所属的会话编号。

    Returns:
        SimpleNamespace: 供外层用例断言使用的预设结果。
    """
    return SimpleNamespace(session_id=session_id)


def test_read_file_supports_line_and_character_ranges(tmp_path: Path, monkeypatch) -> None:
    """验证文件读取支持行范围和字符范围。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    (tmp_path / "sample.txt").write_text("甲行\n乙行\n丙行\n", encoding="utf-8")

    async def scenario() -> tuple[str, str]:
        """由外层测试调用，完成“文件读取支持行范围和字符范围”的异步步骤。

        Returns:
            tuple[str, str]: 供外层用例断言使用的预设结果。
        """
        lines = await read_file._tool_func(
            path="sample.txt", start=1, end=3, unit="line", context=_context()
        )
        chars = await read_file._tool_func(
            path="sample.txt", start=2, end=6, unit="character", context=_context()
        )
        return lines, chars

    lines, chars = asyncio.run(scenario())

    assert lines == "乙行\n丙行"
    assert chars == "\n乙行\n"


def test_read_file_resolves_current_session_artifact() -> None:
    """验证文件读取解析当前会话工具结果引用。"""
    store = ToolResultArtifactStore(get_tool_results_dir())
    reference = store.save("session-a", "第一行\n第二行\n第三行")

    result = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=1,
        end=2,
        unit="line",
        context=_context(),
    ))

    assert result == "第二行"


def test_read_file_execute_uses_default_line_unit(tmp_path: Path, monkeypatch) -> None:
    """验证模型只提供路径时，经装饰器执行仍按默认行单位读取完整文件。

    Args:
        tmp_path (Path): Pytest 提供的临时文件目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): 将工作区临时替换到用例目录。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    (tmp_path / "sample.txt").write_text("第一行\n第二行\n", encoding="utf-8")

    result = asyncio.run(read_file().execute({"path": "sample.txt"}, _context()))

    assert result.text == "第一行\n第二行"


def test_read_file_schema_preserves_nullable_ranges() -> None:
    """验证模型收到的起止位置允许整数或空值，并且仍然可以省略。"""
    parameters = encoded_tool(read_file())["function"]["parameters"]
    for name in ("start", "end"):
        parameter = parameters["properties"][name]
        assert parameter["type"] == "integer"
        assert parameter["nullable"] is True
        assert name not in parameters["required"]
        assert "default" not in parameter


def test_read_file_rejects_invalid_range_and_cross_session_artifact() -> None:
    """验证无效范围和跨会话结果引用被拒绝。"""
    store = ToolResultArtifactStore(get_tool_results_dir())
    reference = store.save("session-a", "secret")

    invalid_range = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=2,
        end=1,
        unit="line",
        context=_context(),
    ))
    cross_session = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=None,
        end=None,
        unit="line",
        context=_context("session-b"),
    ))

    assert invalid_range.startswith("错误:")
    assert cross_session.startswith("错误:")


def test_read_file_disables_oversized_result_persistence() -> None:
    """验证读取工具不再外置保存超长读取结果。"""
    assert read_file.persist_oversized_result is False


def test_read_image_returns_valid_images_as_system_tool_images(tmp_path: Path, monkeypatch) -> None:
    """验证有效图片以内部工具图片返回。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    (tmp_path / "valid.png").write_bytes(png)
    (tmp_path / "invalid.png").write_bytes(b"not-png")

    raw = asyncio.run(read_image._tool_func(paths=["valid.png", "invalid.png"]))
    normalized = ToolResultNormalizer().normalize(raw)

    assert image_kind(normalized) == "tool"
    assert len(image_urls(normalized)) == 1
    assert "文件签名不匹配" in normalized.text


def test_read_image_counts_only_valid_images_toward_limit(tmp_path: Path, monkeypatch) -> None:
    """验证只有有效图片占用数量预算。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    for index in range(5):
        (tmp_path / f"invalid-{index}.png").write_bytes(b"not-png")
    valid = b"\x89PNG\r\n\x1a\n" + b"payload"
    (tmp_path / "valid.png").write_bytes(valid)

    raw = asyncio.run(read_image._tool_func(paths=[
        *(f"invalid-{index}.png" for index in range(5)),
        "valid.png",
    ]))
    normalized = ToolResultNormalizer().normalize(raw)

    assert len(image_urls(normalized)) == 1
    assert image_urls(normalized)[0].startswith("data:image/png;base64,")


def test_exec_command_preserves_output_for_unified_normalization(tmp_path: Path, monkeypatch) -> None:
    """验证终端工具将完整调用结果交给统一工具结果规范化。

    Args:
        tmp_path (Path): 当前测试的隔离工作目录。
        monkeypatch (pytest.MonkeyPatch): 将工具工作目录限制在临时目录。
    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    import app.agent.tools.exec_command as terminal_module
    from app.agent.tools.core.base import ToolContext
    from app.agent.tools.terminal.models import TaskLaunchResult

    monkeypatch.setattr(terminal_module, "WORKDIR", tmp_path)

    class _Launcher:
        """提供隔离测试替身，用于验证：任务运行器输出仍交给统一工具结果规范化。"""
        async def launch(self, session_id, command, argv, *, cwd, tty, yield_time_ms, tool_name,
                         origin_run_id=None, origin_tool_call_id=None):
            """执行该回归场景中的局部测试步骤。

            Args:
                session_id (对象): 共享 Ayaya 会话标识。
                command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。
                argv (对象): 已经构造好的进程参数列表。
                cwd (对象): 命令执行工作目录。
                tty (bool): 是否使用 PTY。
                yield_time_ms (int): 等待预算。
                tool_name (str): 创建任务的工具名。
                origin_run_id (对象): 发起进程任务的运行轮次标识。
                origin_tool_call_id (对象): 发起任务的工具调用标识。
            """
            return TaskLaunchResult("task", False, "x" * 60_000)

    context = ToolContext(
        session_id="s", state=None,
        emit_event=lambda *_args: None,
        get_checkpoint=lambda: {}, set_checkpoint=lambda _value: None,
        task_launcher=_Launcher(),
    )

    result = asyncio.run(terminal_module.ExecCommandTool().execute({"cmd": "echo x"}, context)).text

    # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
    assert result.endswith("x" * 60_000)
    assert "task_id=task" in result


def test_apply_patch_adds_updates_and_deletes_files(tmp_path: Path, monkeypatch) -> None:
    """验证补丁工具完成新增、修改和删除。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    (tmp_path / "update.txt").write_text("before\nkeep\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("obsolete", encoding="utf-8")
    patch = """*** Begin Patch
*** Add File: added.txt
+new file
*** Update File: update.txt
@@
-before
+after
 keep
*** Delete File: delete.txt
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("已应用补丁")
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "new file\n"
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "after\nkeep\n"
    assert not (tmp_path / "delete.txt").exists()


def test_apply_patch_validates_every_operation_before_writing(tmp_path: Path, monkeypatch) -> None:
    """验证补丁全部校验成功后才写入文件。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    patch = """*** Begin Patch
*** Add File: should-not-exist.txt
+content
*** Update File: missing.txt
@@
-before
+after
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("错误:")
    assert not (tmp_path / "should-not-exist.txt").exists()


def test_apply_patch_rejects_workspace_escape(tmp_path: Path, monkeypatch) -> None:
    """验证补丁路径不能越过工作区。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    patch = """*** Begin Patch
*** Add File: ../escape.txt
+content
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("错误:")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_tool_registry_exposes_new_tools_without_internal_modules() -> None:
    """验证工具注册表公开新工具并跳过内部模块。"""
    names = ToolRegistry.list_tools()

    assert "apply_patch" in names
    assert "read_image" in names
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "delete_file" not in names
    assert "result_artifacts" not in names
    assert "result_limits" not in names
    assert "result_normalizer" not in names
