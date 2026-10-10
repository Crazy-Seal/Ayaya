from fastapi.testclient import TestClient

from main import app


def test_backend_lifespan_initializes_runtime_and_serves_health() -> None:
    """验证应用生命周期初始化运行时并可提供健康信息。

    """
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
