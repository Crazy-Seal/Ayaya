"""通过私有管道验证 REPL 消息字段和反序列化边界。"""

import json
from multiprocessing import Pipe

import pytest

from app.repl.protocol import Message, receive, send


@pytest.mark.parametrize("message", [
    {"kind": "execute", "execution_id": "call", "code": "print(1)"},
    {"kind": "initialize", "capabilities": [
        {"module_root": "repl", "module_name": "sample", "export_name": "sample",
         "config": {"nested": [True, None, {"value": 3}]}}]},
    {"kind": "event", "execution_id": "call", "capability": "sample",
     "name": "status_notice", "payload": {"id": "screen", "active": True, "text": "控制中"}},
    {"kind": "interrupt", "execution_id": "call"},
    {"kind": "ready", "execution_id": ""},
    {"kind": "error", "execution_id": "", "error": "初始化失败"},
    {"kind": "interrupt_ack", "execution_id": "call", "accepted": True},
    {"kind": "text", "execution_id": "call", "text": "输出"},
    {"kind": "image", "execution_id": "call", "data": "aW1hZ2U=", "mime_type": "image/png"},
    {"kind": "finished", "execution_id": "call", "reason": "completed", "error": "", "pid": 42},
])
def test_receive_preserves_message_fields(message: Message) -> None:
    """接收固定字段，同时保留插件定义的嵌套配置和事件内容。

    Args:
        message (Message): 发给接收端的完整协议消息。
    """
    reader, writer = Pipe(duplex=False)
    try:
        send(writer, message)
        assert receive(reader) == message
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("message", [
    [],
    {"kind": "unknown"},
    {"kind": "execute", "execution_id": "call"},
    {"kind": "execute", "execution_id": "call", "code": 123},
    {"kind": "text", "execution_id": "call", "text": False},
    {"kind": "interrupt_ack", "execution_id": "call", "accepted": 1},
    {"kind": "finished", "execution_id": "call", "reason": "completed", "error": "", "pid": True},
    {"kind": "finished", "execution_id": "call", "reason": "unknown", "error": "", "pid": 42},
    {"kind": "initialize", "capabilities": "sample"},
    {"kind": "initialize", "capabilities": [
        {"module_root": "repl", "module_name": "sample", "export_name": "sample", "config": []}]},
    {"kind": "event", "execution_id": "call", "capability": "sample", "name": "notice", "payload": []},
])
def test_receive_rejects_invalid_message_fields(message: object) -> None:
    """错误消息在接收边界报错，防止后续执行把错误值当作代码或输出。

    Args:
        message (object): 缺字段、字段类型错误或种类未知的 JSON 数据。
    """
    reader, writer = Pipe(duplex=False)
    try:
        writer.send_bytes(json.dumps(message).encode())
        with pytest.raises(ValueError):
            receive(reader)
    finally:
        reader.close()
        writer.close()
