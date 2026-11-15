"""通过正式子进程验证原生 Python 执行和持久状态。"""

import asyncio
import io
import ctypes
from pathlib import Path

import pytest
from PIL import Image

from app.repl.repl_service import ReplService
from app.repl.contracts import ReplCallContext, ReplEndReason, ReplText
from app.repl.contracts import PreparedReplCapability
from app.repl.repl_api import ReplCapability


def capability_context(value: int = 12) -> ReplCallContext:
    """声明测试能力，供正式 runner 自动初始化。

    Args:
        value (int): 能力初始化参数，用于验证变化后重建。

    Returns:
        ReplCallContext: 带来源插件名和能力声明的调用。
    """
    declaration = ReplCapability(Path(__file__).parent / "fixtures" / "repl_capability",
                                 "ayaya_test_capability", "sample", {"value": value})
    return ReplCallContext("capability", "run", "call", (PreparedReplCapability("test_plugin", declaration),))


def test_repl_preserves_functions_imports_and_state_after_error(tmp_path) -> None:
    """同会话连续执行时保留全局变量，代码异常后仍能继续使用。

    Args:
        tmp_path (Path): 本次子进程专用工作目录。
    """
    async def scenario() -> None:
        """执行多次代码并从公开结果读取保留的状态。"""
        service = ReplService(workdir=tmp_path)
        context = ReplCallContext("session", "run", "call", ())
        try:
            first = await service.execute("import os\nx = [3]\ndef total(): return sum(x)\nprint(total())", context)
            assert first.reason == ReplEndReason.COMPLETED
            assert "".join(item.text for item in first.outputs if isinstance(item, ReplText)) == "3\n"
            failed = await service.execute("x.append(7)\nraise ValueError('测试错误')", context)
            assert failed.reason == ReplEndReason.ERROR
            assert "ValueError: 测试错误" in failed.error
            final = await service.execute("print(total(), os.getpid())", context)
            assert final.reason == ReplEndReason.COMPLETED
            assert "".join(item.text for item in final.outputs if isinstance(item, ReplText)).split() == ["10", str(first.pid)]
        finally:
            await service.close()
    asyncio.run(scenario())


def test_interrupt_during_compilation_prevents_code_side_effects(tmp_path) -> None:
    """编译阶段已受理的中断，在进入模型代码前停止，保留同一进程。

    Args:
        tmp_path (Path): 检查模型代码副作用的隔离目录。
    """
    async def scenario() -> None:
        """让测试能力安排编译期中断，检查后续文件操作没有发生。"""
        service = ReplService(workdir=tmp_path)
        context = capability_context()
        try:
            first = await service.execute("saved = 21\nsample.interrupt_next_compile()", context)
            interrupted = await service.execute("from pathlib import Path\nPath('unexpected').touch()", context)
            assert interrupted.reason == ReplEndReason.INTERRUPTED
            assert not (tmp_path / "unexpected").exists()
            after = await service.execute("print(saved)", context)
            assert after.pid == first.pid
            assert after.reason == ReplEndReason.COMPLETED
        finally:
            await service.close()
    asyncio.run(scenario())


def test_next_call_rebuilds_runner_that_crashed_while_idle(tmp_path) -> None:
    """代码返回后进程意外退出，下一调用直接重建并执行新代码。

    Args:
        tmp_path (Path): 独立工作目录。
    """
    async def scenario() -> None:
        """等待操作系统确认旧进程退出，再进行下一次执行。"""
        service = ReplService(workdir=tmp_path)
        context = ReplCallContext("idle-crash", "run", "call", ())
        try:
            first = await service.execute("import threading, os\nx = 1\nthreading.Timer(0.1, os._exit, args=(9,)).start()", context)
            assert first.pid is not None
            async with asyncio.timeout(3):
                while process_is_running(first.pid):
                    await asyncio.sleep(0.01)
            after = await service.execute("print('x' in globals())", context)
            assert after.reason == ReplEndReason.COMPLETED, after.error
            assert after.pid != first.pid
            assert "".join(item.text for item in after.outputs) == "False\n"
        finally:
            await service.close()
    asyncio.run(scenario())


def test_close_waits_for_application_event_cleanup_before_return(tmp_path) -> None:
    """应用关闭等待当前执行的事件收尾，消费者可在之后安全退出。

    Args:
        tmp_path (Path): 进程执行屏障目录。
    """
    from app.repl.contracts import ReplExecutionFinished

    async def scenario() -> None:
        """用公开事件接入函数的屏障验证 close 的完成边界。"""
        entered, release = asyncio.Event(), asyncio.Event()

        async def events(event) -> None:
            """暂停应用层本次结束处理。

            Args:
                event (ReplExecutionFinished | ReplPluginEvent): REPL 交付的事件。
            """
            if isinstance(event, ReplExecutionFinished):
                entered.set()
                await release.wait()

        service = ReplService(workdir=tmp_path, on_event=events)
        running = asyncio.create_task(service.execute("from pathlib import Path\nPath('running').touch()\nwhile True: pass", ReplCallContext("s", "run", "call", ())))
        closing = None
        try:
            await wait_for_file(tmp_path / "running")
            closing = asyncio.create_task(service.close())
            await asyncio.wait_for(entered.wait(), 3)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(closing, 3)
            await running
        finally:
            release.set()
            await service.close()
            await asyncio.gather(running, *(tuple([closing]) if closing else ()), return_exceptions=True)
    asyncio.run(scenario())


def test_native_wait_defers_interrupt_until_return_then_keeps_state(tmp_path) -> None:
    """不可中断的原生等待返回后交付中断，已有状态仍然可用。

    Args:
        tmp_path (Path): 独立进程工作目录。
    """
    async def scenario() -> None:
        """调用 Windows Sleep 模拟原生阻塞，确认后续业务语句被阻止。"""
        service = ReplService(workdir=tmp_path)
        context = capability_context()
        try:
            result = await service.execute("import ctypes\nsaved = 17\nsample.interrupt_later(0.03)\nctypes.windll.kernel32.Sleep(250)\nsample.host.raise_if_interrupted(sample.host.execution_id)\nprint('不应执行')", context)
            assert result.reason == ReplEndReason.INTERRUPTED
            assert not result.outputs
            after = await service.execute("print(saved)", context)
            assert after.pid == result.pid
            assert "".join(item.text for item in after.outputs) == "17\n"
        finally:
            await service.close()
    asyncio.run(scenario())


def process_is_running(pid: int) -> bool:
    """从 Windows 查询进程是否仍在执行，验证实际进程树回收。

    Args:
        pid (int): 由模型代码报告的进程编号。

    Returns:
        bool: 目标进程仍然存活时为 True。
    """
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        assert result in {0, 258}
        return result == 258
    finally:
        kernel.CloseHandle(handle)


def test_close_during_execution_reaps_runner_and_its_children(tmp_path) -> None:
    """应用关闭正在执行的 REPL 时，收齐故障结果并结束全部子进程。

    Args:
        tmp_path (Path): 子进程报告文件和工作目录。
    """
    async def scenario() -> None:
        """启动真实父子进程后关闭，通过操作系统确认没有残留。"""
        service = ReplService(workdir=tmp_path)
        code = ("import subprocess, sys, os, time\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "Path('processes').write_text(str(os.getpid()) + ' ' + str(child.pid))\nwhile True: time.sleep(1)")
        task = asyncio.create_task(service.execute(code, ReplCallContext("close", "run", "call", ())))
        try:
            await wait_for_file(tmp_path / "processes")
            pids = [int(value) for value in (tmp_path / "processes").read_text().split()]
            assert all(process_is_running(pid) for pid in pids)
            await service.close()
            result = await asyncio.wait_for(task, 3)
            assert result.reason == ReplEndReason.CRASHED
            assert not any(process_is_running(pid) for pid in pids)
        finally:
            await service.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_host_interrupt_cleanup_and_stale_callback_do_not_affect_next_execution(tmp_path) -> None:
    """能力发起中断后保留进程，旧编号和重复回调不能影响后续调用。

    Args:
        tmp_path (Path): 独立工作目录。
    """
    async def scenario() -> None:
        """使用正式 host 模拟 ESC，随后反复调用旧回调。"""
        service = ReplService(workdir=tmp_path)
        context = capability_context()
        try:
            first = await service.execute("saved = 8\nsample.cleanup('released')\nsample.interrupt_later(0.05)\nimport time\ntime.sleep(60)\nprint('不应执行')", context)
            assert first.reason == ReplEndReason.INTERRUPTED
            assert "结束本轮" in first.error
            assert "".join(item.text for item in first.outputs) == "released\n"
            for _ in range(20):
                after = await service.execute("sample.interrupt_old()\nsample.interrupt_old()\nprint(saved)", context)
                assert after.pid == first.pid
                assert after.reason == ReplEndReason.COMPLETED
                assert "".join(item.text for item in after.outputs) == "8\n"
        finally:
            await service.close()
    asyncio.run(scenario())


def test_initialization_failure_cancel_and_timeout_release_partial_runner(tmp_path) -> None:
    """能力初始化失败、取消和超时后，下一次执行可重新初始化。

    Args:
        tmp_path (Path): 初始化到达标记和工作目录。
    """
    from dataclasses import replace

    async def scenario() -> None:
        """通过公开执行结果检查半初始化资源得到释放。"""
        service = ReplService(workdir=tmp_path, initialization_timeout=2)
        context = capability_context()
        prepared = context.capabilities[0]
        bad = replace(context, capabilities=(replace(prepared, declaration=replace(prepared.declaration, config={"fail": True})),))
        marker = tmp_path / "initializing"
        slow = replace(context, capabilities=(replace(prepared, declaration=replace(prepared.declaration, config={"initialization_marker": str(marker)})),))
        try:
            failure = await service.execute("print('不应执行')", bad)
            assert failure.reason == ReplEndReason.CRASHED
            assert "测试初始化失败" in failure.error
            task = asyncio.create_task(service.execute("print('不应执行')", slow))
            await wait_for_file(marker)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            timed = await service.execute("print('不应执行')", slow)
            assert timed.reason == ReplEndReason.TIMEOUT
            normal = await service.execute("sample.inspect()", context)
            assert normal.reason == ReplEndReason.COMPLETED
        finally:
            await service.close()
    asyncio.run(scenario())


def test_capability_is_injected_and_cleanup_runs_before_return(tmp_path) -> None:
    """能力无需手动导入，观察直接输出图片，清理故障不阻止其他清理。

    Args:
        tmp_path (Path): 独立工作目录。
    """
    from app.repl.contracts import ReplImage

    async def scenario() -> None:
        """先验证清理及状态复用，再改变能力配置验证重建。"""
        service = ReplService(workdir=tmp_path)
        context = capability_context()
        try:
            first = await service.execute("sample.inspect()\nsample.observe()\nsample.cleanup('first')\nsample.cleanup('last', True)\nx = 20", context)
            assert first.reason == ReplEndReason.COMPLETED, first.error
            assert any(isinstance(item, ReplImage) for item in first.outputs)
            text = "".join(item.text for item in first.outputs if isinstance(item, ReplText))
            assert text == "12 False\nlast\nfirst\n"
            assert "测试清理失败" in first.error
            same = await service.execute("print(x)", context)
            assert same.pid == first.pid
            assert "".join(item.text for item in same.outputs) == "20\n"
            changed = await service.execute("sample.inspect()\nprint('x' in globals())", capability_context(18))
            assert changed.pid != first.pid
            assert "".join(item.text for item in changed.outputs) == "18 False\nFalse\n"
        finally:
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("image_expression", ["image", "encoded.getvalue()", "jpeg.getvalue()"])
def test_repl_outputs_images_in_order_and_collects_native_streams(tmp_path, image_expression) -> None:
    """显式图片夹在文字之间，第三方原生输出也进入同一次结果。

    Args:
        tmp_path (Path): 独立工作目录。
        image_expression (str): 模型传入 PIL、PNG 或 JPEG 的表达式。
    """
    from app.repl.contracts import ReplImage

    async def scenario() -> None:
        """从实际编码结果解码检查图片内容。"""
        service = ReplService(workdir=tmp_path)
        context = ReplCallContext("images", "run", "call", ())
        try:
            result = await service.execute(
                "from PIL import Image\nimport io, os, sys\n"
                "image = Image.new('RGB', (4, 3), (0, 255, 0))\n"
                "encoded = io.BytesIO(); image.save(encoded, format='PNG')\n"
                "jpeg = io.BytesIO(); image.save(jpeg, format='JPEG')\n"
                "print('before')\n"
                f"display_image({image_expression})\n"
                "print('after')\nprint('stderr-value', file=sys.stderr)\n"
                "sys.stdout.buffer.write(b'buffer-out\\n')\n"
                "import ctypes\nctypes.CDLL('ucrtbase').puts(b'crt-out')\n"
                "os.write(1, b'native-out\\n'); os.write(2, b'native-error\\n')", context)
            assert result.reason == ReplEndReason.COMPLETED, result.error
            position = next(i for i, item in enumerate(result.outputs) if isinstance(item, ReplImage))
            assert "".join(item.text for item in result.outputs[:position] if isinstance(item, ReplText)) == "before\n"
            text = "".join(item.text for item in result.outputs[position + 1:] if isinstance(item, ReplText))
            assert text.startswith("after\nstderr-value\n")
            assert "native-out\n" in text and "native-error\n" in text
            assert "buffer-out\n" in text and "crt-out\n" in text
            picture = result.outputs[position]
            with Image.open(io.BytesIO(picture.data)) as decoded:
                assert decoded.size == (4, 3)
                assert decoded.getpixel((0, 0))[1] >= 250
        finally:
            await service.close()
    asyncio.run(scenario())


async def wait_for_file(path: Path) -> None:
    """等待测试代码写出到达标记，避免猜测子进程启动耗时。

    Args:
        path (Path): 子进程将在指定执行位置创建的文件。
    """
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.01)


def test_repl_rejects_busy_calls_rebuilds_and_recovers_from_timeout(tmp_path) -> None:
    """忙时立即失败，超时回收状态，会话变化及 reset 清空命名空间。

    Args:
        tmp_path (Path): 本次测试的临时工作目录。
    """
    async def scenario() -> None:
        """通过执行结果和并发调用观察进程生命周期。"""
        service = ReplService(workdir=tmp_path, execution_timeout=1.0)
        context = ReplCallContext("first", "run", "call", ())
        try:
            running = asyncio.create_task(service.execute("from pathlib import Path\nPath('started').touch()\nwhile True: pass", context))
            await wait_for_file(tmp_path / "started")
            with pytest.raises(RuntimeError, match="正在执行"):
                await service.execute("print('不应执行')", context)
            with pytest.raises(RuntimeError, match="正在执行"):
                await service.reset()
            timed_out = await running
            assert timed_out.reason == ReplEndReason.TIMEOUT
            first = await service.execute("x = 8\nprint(x)", context)
            await service.reset()
            reset = await service.execute("print('x' in globals())", context)
            assert reset.pid != first.pid
            assert "".join(item.text for item in reset.outputs) == "False\n"
            await service.execute("x = 9", context)
            switched = await service.execute("print('x' in globals())", ReplCallContext("second", "run", "call", ()))
            assert switched.pid != reset.pid
            assert "".join(item.text for item in switched.outputs) == "False\n"
        finally:
            await service.close()
        with pytest.raises(RuntimeError, match="关闭"):
            await service.execute("print(1)", context)
    asyncio.run(scenario())


@pytest.mark.parametrize("blocking_code", ["while True: pass", "time.sleep(60)"])
def test_cancel_interrupts_code_and_preserves_process_and_namespace(tmp_path, blocking_code) -> None:
    """取消等待会中断循环或 sleep，同一个进程随后仍能使用既有状态。

    Args:
        tmp_path (Path): 到达标记和工作目录。
        blocking_code (str): 需要被中断的模型代码。
    """
    async def scenario() -> None:
        """等待真实执行到达标记后取消，再通过下一调用核验。"""
        service = ReplService(workdir=tmp_path)
        context = ReplCallContext("cancel", "run", "call", ())
        try:
            initial = await service.execute("import os, time\nfrom pathlib import Path\nvalues = [4]", context)
            task = asyncio.create_task(service.execute("values.append(6)\nPath('running').touch()\n" + blocking_code, context))
            await wait_for_file(tmp_path / "running")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            after = await service.execute("print(sum(values), os.getpid())", context)
            assert after.reason == ReplEndReason.COMPLETED, after.error
            assert after.pid == initial.pid
            assert "".join(item.text for item in after.outputs).split() == ["10", str(initial.pid)]
        finally:
            await service.close()
    asyncio.run(scenario())
