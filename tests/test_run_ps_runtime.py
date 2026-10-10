"""检查直接 PowerShell 启动和隔离后台任务的实时输出。"""

import asyncio
import os
from pathlib import Path

import pytest

from app.agent.tools.run_ps import _build_command
from app.schemas.runtime_types import TaskStatus
from app.services.task_supervisor import TaskSupervisor


def test_power_shell_command_is_direct_and_preserves_utf8_setup() -> None:
    """检查真实启动参数保留非交互和 UTF-8 设置，命令无需 Conda 查找。"""
    argv = _build_command("Write-Output '中文'")
    assert argv[:6] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
    assert "UTF8Encoding" in argv[-1]
    assert "[Console]::OutputEncoding" in argv[-1]
    assert argv[-1].endswith("Write-Output '中文'")
    assert "conda" not in " ".join(argv).lower()


@pytest.mark.skipif(os.name != "nt", reason="直接 PowerShell 进程树仅在 Windows 验证")
@pytest.mark.parametrize("background", [False, True])
def test_direct_power_shell_output_is_visible_before_exit_and_survives_stop(
    tmp_path: Path, background: bool,
) -> None:
    """前台超时移交和显式后台都能及时读到中文，停止后日志仍完整保留。

    Args:
        tmp_path (Path): Pytest 分配的隔离任务目录和数据库路径。
        background (bool): 是否显式后台；否则立即超时移交同一个进程。
    """
    async def scenario() -> None:
        """运行无外部副作用的输出和等待命令，最后停止受管进程树。"""
        supervisor = TaskSupervisor(
            db_path=str(tmp_path / "state.sqlite3"), spool_dir=tmp_path / "tasks",
        )
        command = "[Console]::WriteLine('中文实时输出'); [Console]::Out.Flush(); Start-Sleep -Seconds 30"
        try:
            # 1. 使用工具真实构造的 PowerShell 参数，不用 Python 进程绕过启动路径。
            launched = await supervisor.launch(
                "s", command, _build_command(command), cwd=str(tmp_path),
                background=background, timeout_sec=0.01,
            )
            assert launched.background
            deadline = asyncio.get_running_loop().time() + 8
            while True:
                page = await supervisor.read_output(launched.task_id, 0, 4096)
                if "中文实时输出" in page["text"]:
                    break
                assert asyncio.get_running_loop().time() < deadline, "命令仍在等待时应已保存输出"
                await asyncio.sleep(0.02)
            record = await supervisor.get(launched.task_id)
            assert record.status == TaskStatus.RUNNING
            assert record.background
            # 2. 显式停止同一个任务，宽限结束后原输出仍能从持久日志读取。
            assert await supervisor.stop(launched.task_id)
            deadline = asyncio.get_running_loop().time() + 6
            while (await supervisor.get(launched.task_id)).status == TaskStatus.RUNNING:
                assert asyncio.get_running_loop().time() < deadline, "受管 PowerShell 应在宽限后结束"
                await asyncio.sleep(0.02)
            assert (await supervisor.get(launched.task_id)).status == TaskStatus.STOPPED
            assert "中文实时输出" in (await supervisor.read_output(launched.task_id, 0, 4096))["text"]
        finally:
            await supervisor.close()

    asyncio.run(scenario())
