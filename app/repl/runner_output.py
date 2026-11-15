"""收集 Python、图片及原生文件描述符输出，串行交付完整消息。"""

import base64
import codecs
import ctypes
import io
import os
import queue
import threading
import uuid
from multiprocessing.connection import PipeConnection

from protocol import MessageKind, ResultMessage, send


class ResultWriter:
    """让独立写线程完整发送结果，主线程中断不切断消息帧。"""

    def __init__(self, connection: PipeConnection) -> None:
        """启动唯一管道写线程。

        Args:
            connection (PipeConnection): 结果管道的写端。
        """
        self.connection = connection
        self.execution_id = ""
        self.messages: queue.Queue[ResultMessage] = queue.Queue()
        threading.Thread(target=self._write, daemon=True).start()

    def _write(self) -> None:
        """按队列顺序发送消息，父进程断开后退出子进程。"""
        try:
            while True:
                message = self.messages.get()
                send(self.connection, message)
                self.messages.task_done()
        except (OSError, EOFError):
            os._exit(1)

    def flush(self) -> None:
        """退出前等待已排队的初始化故障消息完成发送。"""
        self.messages.join()

    def emit(self, message: ResultMessage) -> None:
        """将调用方已绑定执行编号的完整结果消息交给写线程。

        Args:
            message (ResultMessage): 带执行编号和对应种类全部字段的输出或状态消息。
        """
        self.messages.put(message)

    def text(self, text: str) -> None:
        """把当前执行产生的文字加入结果。

        Args:
            text (str): 待交付的文字片段。
        """
        if text and self.execution_id:
            self.emit({"kind": MessageKind.TEXT, "execution_id": self.execution_id, "text": text})

    def image(self, data: bytes) -> None:
        """检查已编码图片后沿同一队列输出。

        Args:
            data (bytes): PNG 或 JPEG 图片文件编码。

        Raises:
            ValueError: 图片不是支持的 PNG/JPEG 编码。
        """
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("display_image 需要 PIL Image 或 PNG/JPEG bytes")
            mime = "image/png" if image.format == "PNG" else "image/jpeg"
            image.verify()
        self.emit({"kind": MessageKind.IMAGE, "execution_id": self.execution_id,
                   "data": base64.b64encode(data).decode("ascii"), "mime_type": mime})

    def display_image(self, image: object) -> None:
        """编码模型传入的 PIL 图片，或直接输出 PNG/JPEG bytes。

        Args:
            image (object): PIL Image 或已编码的 PNG/JPEG bytes。

        Raises:
            TypeError: 参数类型不受支持。
        """
        from PIL import Image
        if isinstance(image, Image.Image):
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
            self.image(encoded.getvalue())
        elif isinstance(image, bytes):
            self.image(image)
        else:
            raise TypeError("display_image 需要 PIL Image 或 PNG/JPEG bytes")


class TextOutput(io.TextIOBase):
    """把 Python 文字直接送入结果队列，保留与显式图片的先后顺序。"""

    encoding = "utf-8"

    def __init__(self, writer: ResultWriter, descriptor: int) -> None:
        """保存结果出口和对应原生流的描述符。

        Args:
            writer (ResultWriter): 当前 runner 的唯一结果出口。
            descriptor (int): stdout 为 1，stderr 为 2。
        """
        self.writer = writer
        self.descriptor = descriptor
        self.buffer = io.FileIO(descriptor, mode="wb", closefd=False)

    def writable(self) -> bool:
        """让库确认当前文字流可以写入。

        Returns:
            bool: 本流支持写入。
        """
        return True

    def fileno(self) -> int:
        """返回已接入原生输出收集的文件描述符。

        Returns:
            int: stdout 或 stderr 的文件描述符。
        """
        return self.descriptor

    def write(self, text: str) -> int:
        """交付一段文字并返回字符数。

        Args:
            text (str): 模型代码或库产生的文字。

        Returns:
            int: 已交付字符数。
        """
        self.writer.text(text)
        return len(text)


class NativeOutput:
    """收集直接写入 stdout/stderr 的字节，并在完成前等待收齐。"""

    def __init__(self, writer: ResultWriter, descriptor: int) -> None:
        """用匿名管道接管一个原生输出流。

        Args:
            writer (ResultWriter): 原生文字交付的结果出口。
            descriptor (int): 被接管的 stdout 或 stderr。
        """
        self.writer = writer
        self.descriptor = descriptor
        self.marker = b"\x00ayaya-drain-" + uuid.uuid4().hex.encode() + b"\x00"
        self.drained = threading.Event()
        self.reader, write_end = os.pipe()
        os.dup2(write_end, descriptor)
        os.close(write_end)
        # 1. Python 文件描述符和 Win32 标准句柄指向同一管道，原生库及其子进程共用出口。
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetStdHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        kernel.SetStdHandle.restype = wintypes.BOOL
        if not kernel.SetStdHandle(-11 if descriptor == 1 else -12, msvcrt.get_osfhandle(descriptor)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.crt = ctypes.CDLL("ucrtbase")
        self.crt.fflush.argtypes = [ctypes.c_void_p]
        self.crt.fflush.restype = ctypes.c_int
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        """持续解码字节，在本流排空标记前交付已收到的内容。"""
        pending = b""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while data := os.read(self.reader, 65536):
            pending += data
            position = pending.find(self.marker)
            if position >= 0:
                self.writer.text(decoder.decode(pending[:position]))
                pending = pending[position + len(self.marker):]
                self.drained.set()
            elif len(pending) >= len(self.marker):
                boundary = len(pending) - len(self.marker) + 1
                self.writer.text(decoder.decode(pending[:boundary]))
                pending = pending[boundary:]

    def drain(self) -> None:
        """让完成消息排在本次原生字节之后，等待收集线程确认。"""
        self.drained.clear()
        self.crt.fflush(None)
        os.write(self.descriptor, self.marker)
        self.drained.wait()
