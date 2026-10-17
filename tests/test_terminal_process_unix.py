"""仅依赖标准库的 Unix Adapter 验收，可在 WSL 中直接执行。"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from app.agent.tools.terminal.process import spawn_terminal


@unittest.skipUnless(os.name == "posix", "Unix 控制终端只在 Unix 主机验证")
class UnixTerminalProcessTests(unittest.IsolatedAsyncioTestCase):
    """跨内部进程 Interface 验证真实控制终端和进程组清理。"""

    async def _check_background_jobs_are_reclaimed(self, exit_shell: bool) -> None:
        """检查 Bash 后台作业在 Shell 退出或强停后结束，且不影响其他终端。

        Args:
            exit_shell (bool): 为真时让 Shell 自行退出，否则先请求停止再强停。
        """
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("当前 Unix 主机没有 Bash")
        with tempfile.TemporaryDirectory(prefix="ayaya-unix-jobs-") as directory:
            process = await spawn_terminal([bash, "--noprofile", "--norc", "-i"], directory, True)
            unrelated = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True,
            )
            chunks = []
            changed = asyncio.Event()
            children = []

            async def drain() -> None:
                """持续排空测试终端；收到提示符或后台作业编号时唤醒测试流程。"""
                while chunk := await process.read(65536):
                    chunks.append(chunk)
                    changed.set()

            async def wait_for_jobs() -> None:
                """等待 Bash 实际输出两个后台 PID，不把命令回显当成运行结果。"""
                while True:
                    changed.clear()
                    output = b"".join(chunks)
                    match = re.search(rb"\r?\nJOBS:(\d+),(\d+)\r?\n", output)
                    if match and not children:
                        children.extend(int(value) for value in match.groups())
                    ready = exit_shell or len(re.findall(rb"JOB_READY\r?\n", output)) == 2
                    if match and ready:
                        return
                    await changed.wait()

            reader = asyncio.create_task(drain())
            waiter = asyncio.create_task(process.wait())
            try:
                # 1. 先等 Shell 提示符，再发送命令，避免启动时的终端初始化吞掉输入。
                await asyncio.wait_for(changed.wait(), 5)
                job = ("sleep 60" if exit_shell else
                       "bash -c 'trap \"\" HUP TERM; printf \"\\nJOB_READY\\n\"; exec sleep 60'")
                command = (f"{job} & first=$!; {job} & second=$!; "
                           "printf '\\nJOBS:%s,%s\\n' \"$first\" \"$second\"; "
                           + ("exit" if exit_shell else ":") + "\n")
                await process.write(command.encode())
                try:
                    await asyncio.wait_for(wait_for_jobs(), 5)
                except asyncio.TimeoutError as exc:
                    raise AssertionError(f"后台作业未就绪，终端输出：{b''.join(chunks)!r}") from exc
                # 2. 停止路径包含忽略温和信号的作业，验证宽限期后的强停能清理独立进程组。
                if not exit_shell:
                    await process.interrupt()
                    process.kill()
                await asyncio.wait_for(asyncio.shield(waiter), 5)
                await asyncio.wait_for(asyncio.shield(reader), 3)
                for child in children:
                    result = subprocess.run(["ps", "-p", str(child), "-o", "stat="],
                                            capture_output=True, text=True, check=False)
                    self.assertTrue(not result.stdout.strip() or result.stdout.lstrip().startswith("Z"),
                                    f"后台作业 {child} 仍然运行")
                self.assertIsNone(unrelated.returncode, "清理当前终端不能终止其他操作系统会话")
            finally:
                # 3. 即便回归失败，也独立清理已发现的作业，避免挂起的 read 线程阻止测试退出。
                for child in children:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.kill()
                await asyncio.wait_for(asyncio.shield(waiter), 5)
                await asyncio.wait_for(asyncio.shield(reader), 5)
                process.close()
                if unrelated.returncode is None:
                    unrelated.kill()
                await unrelated.wait()

    async def test_pty_shell_exit_reclaims_background_job_groups(self) -> None:
        """Shell 正常退出后，后台作业不能继续持有 PTY，输出必须读到 EOF。"""
        await self._check_background_jobs_are_reclaimed(True)

    async def test_pty_force_stop_reclaims_background_job_groups(self) -> None:
        """强停会清理忽略温和信号的后台作业，并保留其他会话的进程。"""
        await self._check_background_jobs_are_reclaimed(False)

    async def test_real_controlling_terminal_and_duplex_io(self) -> None:
        """PTY 子进程拥有控制终端、独立会话，输入可持续发送。"""
        with tempfile.TemporaryDirectory(prefix="ayaya-unix-pty-") as directory:
            code = (
                "import os; print('TTY',os.isatty(0),os.tcgetpgrp(0)==os.getpgrp(),os.getsid(0)==os.getpid(),flush=True); "
                "value=input(); print('RESULT:'+value,flush=True)"
            )
            process = await spawn_terminal([sys.executable, "-u", "-c", code], directory, True)
            waiter = asyncio.create_task(process.wait())
            try:
                output = b""
                while b"TTY True True True" not in output:
                    output += await asyncio.wait_for(process.read(65536), 5)
                await process.write(b"hello\n")
                while chunk := await asyncio.wait_for(process.read(65536), 5):
                    output += chunk
                self.assertIn(b"RESULT:hello", output)
                self.assertEqual(await asyncio.wait_for(waiter, 5), 0)
            finally:
                process.kill()
                await asyncio.wait_for(waiter, 5)
                process.close()

    async def test_pty_ctrl_c_interrupts_foreground_and_keeps_process(self) -> None:
        """Ctrl+C 经过终端行规程中断等待，程序捕获后仍能继续交互。"""
        with tempfile.TemporaryDirectory(prefix="ayaya-unix-ctrl-") as directory:
            code = (
                "print('READY',flush=True)\ntry:\n input()\nexcept KeyboardInterrupt:\n print('INTERRUPTED',flush=True)\n"
                "value=input()\nprint('RESULT:'+value,flush=True)"
            )
            process = await spawn_terminal([sys.executable, "-u", "-c", code], directory, True)
            waiter = asyncio.create_task(process.wait())
            try:
                output = b""
                while b"READY" not in output:
                    output += await asyncio.wait_for(process.read(65536), 5)
                await process.write(b"\x03")
                while b"INTERRUPTED" not in output:
                    output += await asyncio.wait_for(process.read(65536), 5)
                await process.write(b"again\n")
                while chunk := await asyncio.wait_for(process.read(65536), 5):
                    output += chunk
                self.assertIn(b"RESULT:again", output)
                self.assertEqual(await asyncio.wait_for(waiter, 5), 0)
            finally:
                process.kill()
                await asyncio.wait_for(waiter, 5)
                process.close()

    async def test_pipe_root_exit_reclaims_child_holding_stdout(self) -> None:
        """根进程退出后回收仍持有输出管道的同组子进程，避免收尾永久等待。"""
        with tempfile.TemporaryDirectory(prefix="ayaya-unix-tree-") as directory:
            code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print('ROOT_DONE',flush=True)"
            process = await spawn_terminal([sys.executable, "-u", "-c", code], directory, False)
            waiter = asyncio.create_task(process.wait())
            try:
                output = b""
                while chunk := await asyncio.wait_for(process.read(65536), 5):
                    output += chunk
                self.assertIn(b"ROOT_DONE", output)
                self.assertEqual(await asyncio.wait_for(waiter, 5), 0)
            finally:
                process.kill()
                await asyncio.wait_for(waiter, 5)
                process.close()


if __name__ == "__main__":
    unittest.main()
