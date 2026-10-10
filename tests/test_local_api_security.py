from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.local_api import install_local_api_security


TEST_TOKEN = "t" * 43


def _secured_app(call_counter: list[str]) -> FastAPI:
    """创建启用本机令牌及来源检查的测试应用。

    Args:
        call_counter (list[str]): 共享调用计数记录，用于验证是否发生重试或重复调用。

    Returns:
        FastAPI: 供外层用例断言使用的预设结果。
    """
    app = FastAPI()
    install_local_api_security(app, TEST_TOKEN)

    @app.get("/")
    def root():
        """返回无需令牌的测试根路径响应。"""
        return {"ok": True}

    @app.post("/sensitive")
    def sensitive():
        """返回受本机认证保护的测试响应。"""
        call_counter.append("called")
        return {"ok": True}

    return app


def test_sensitive_api_requires_valid_bearer_token() -> None:
    """验证受保护 API 必须提供有效 Bearer 令牌。"""
    calls: list[str] = []
    client = TestClient(_secured_app(calls), base_url="http://127.0.0.1")

    assert client.post("/sensitive").status_code == 401
    assert client.post(
        "/sensitive", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    assert calls == []

    accepted = client.post(
        "/sensitive", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    assert accepted.status_code == 200
    assert calls == ["called"]


def test_local_api_rejects_untrusted_host_and_origin() -> None:
    """验证本机 API 拒绝不可信 Host 和 Origin。"""
    calls: list[str] = []
    client = TestClient(_secured_app(calls), base_url="http://127.0.0.1")
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}

    assert client.post(
        "http://evil.example/sensitive", headers=auth
    ).status_code == 400
    assert client.post(
        "/sensitive", headers={**auth, "Origin": "http://evil.example"}
    ).status_code == 403
    assert calls == []


def test_public_root_is_the_only_token_free_endpoint() -> None:
    """验证只有根路径允许无令牌访问。"""
    client = TestClient(_secured_app([]), base_url="http://127.0.0.1")

    assert client.get("/").status_code == 200
    assert client.get("/openapi.json").status_code == 401
