"""仅由 Unix PTY Adapter 执行的控制终端启动程序。"""

import os
import sys


def main() -> None:
    """在独立子进程中建立会话和控制终端，然后替换为实际命令。"""
    import fcntl
    import termios

    # 1. 此程序已经是独立进程，可以安全设置会话，不在多线程后端的 fork 回调内执行。
    slave = int(sys.argv[1])
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(slave, os.getpgrp())
    if slave > 2:
        os.close(slave)
    # 2. exec 保持任务 PID 不变，退出码由原来的 asyncio 子进程句柄等待。
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
