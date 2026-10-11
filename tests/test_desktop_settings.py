"""通过公开读写接口验证桌面 YAML 迁移，不读取生产前端配置。"""

import json

from app.crud.desktop_settings_dao import DesktopSettingsDao


def test_invalid_yaml_is_not_overwritten_and_failed_save_keeps_previous(tmp_path, monkeypatch) -> None:
    """验证坏配置不会回退覆盖，替换失败时保留已经保存的配置。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): 替换操作系统或环境依赖，用例结束后自动恢复。
    """
    import pytest
    from pathlib import Path
    from pydantic import ValidationError
    from app.schemas.desktop import DesktopSettings
    dao = DesktopSettingsDao(tmp_path / "desktop.yaml", legacy_file=tmp_path / "absent.json")
    dao.config_file.write_text("hide_on_screenshot: bad", encoding="utf-8")
    with pytest.raises(ValidationError):
        dao.load()
    dao.config_file.unlink()
    dao.save(DesktopSettings(hide_on_screenshot=False))
    original = dao.config_file.read_text(encoding="utf-8")

    def fail_replace(_source, _target):
        """模拟操作系统拒绝原子替换。

        Args:
            _source (Path): 尚未替换目标的隔离临时配置文件。
            _target (Path): 本次原子替换的隔离配置目标。
        """
        raise OSError("替换失败")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        dao.save(DesktopSettings())
    assert dao.config_file.read_text(encoding="utf-8") == original


def test_settings_routes_require_auth_and_return_saved_yaml(tmp_path) -> None:
    """通过配置 HTTP seam 验证认证、保存、读回和退出门禁。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.desktop import router, get_desktop_settings_service
    from app.services.desktop_settings_service import DesktopSettingsService
    from app.security.local_api import install_local_api_security
    accepting = [True]
    service = DesktopSettingsService(DesktopSettingsDao(tmp_path / "desktop.yaml", legacy_file=tmp_path / "absent"), lambda: accepting[0])
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_desktop_settings_service] = lambda: service
    install_local_api_security(app, "t" * 43)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/desktop_settings").status_code == 401
        client.headers["Authorization"] = "Bearer " + "t" * 43
        assert client.put("/desktop_settings", json={"hide_on_screenshot": False}).json()["data"] == {"hide_on_screenshot": False}
        assert client.get("/desktop_settings").json()["data"]["hide_on_screenshot"] is False
        assert client.put("/desktop_settings", json={"hide_on_screenshot": "false"}).status_code == 422
        accepting[0] = False
        assert client.put("/desktop_settings", json={"hide_on_screenshot": True}).status_code == 409


def test_legacy_value_migrates_without_changing_window_settings(tmp_path) -> None:
    """首次读取导入旧隐藏设置，后续 YAML 优先且旧窗口字段保持。

    Args:
        tmp_path (Path): 本用例唯一的隔离配置目录。
    """
    legacy = tmp_path / "frontend_settings.json"
    original = {"hide_on_screenshot": False, "chat_window_pinned": True,
                "chat_window_bounds": {"x": 100, "y": 200, "width": 560, "height": 720}}
    legacy.write_text(json.dumps(original), encoding="utf-8")
    dao = DesktopSettingsDao(tmp_path / "desktop_settings.yaml", legacy_file=legacy)
    assert dao.load().hide_on_screenshot is False
    legacy.write_text(json.dumps({**original, "hide_on_screenshot": True}), encoding="utf-8")
    assert dao.load().hide_on_screenshot is False
    assert json.loads(legacy.read_text(encoding="utf-8"))["chat_window_bounds"] == original["chat_window_bounds"]


def test_test_paths_reject_production_overrides(tmp_path, monkeypatch) -> None:
    """测试目录选择不能通过新配置或前端迁移环境变量逃出隔离目录。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
        monkeypatch (pytest.MonkeyPatch): 替换操作系统或环境依赖，用例结束后自动恢复。
    """
    import pytest
    from app.runtime import get_desktop_settings_file, get_legacy_frontend_settings_file
    monkeypatch.setenv("AYAYA_DATA_DIR", str(tmp_path / "isolated"))
    monkeypatch.setenv("AYAYA_DESKTOP_SETTINGS_FILE", str(tmp_path / "outside.yaml"))
    with pytest.raises(RuntimeError):
        get_desktop_settings_file()
    monkeypatch.delenv("AYAYA_DESKTOP_SETTINGS_FILE")
    monkeypatch.setenv("AYAYA_UI_DATA_DIR", str(tmp_path / "outside"))
    with pytest.raises(RuntimeError):
        get_legacy_frontend_settings_file()


def test_window_registration_route_is_authenticated_and_rejects_shutdown() -> None:
    """登记是主进程认证接口，退出状态和未知窗口角色不能进入桌面模块。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from app.routes.desktop import router, get_desktop_controller, get_runtime_scheduler
    from app.schemas.runtime_types import RuntimePhase
    from app.security.local_api import install_local_api_security

    class Desktop:
        """观察路由已验证的完整身份，实际归属另由控制器 seam 测试。"""
        async def register_windows(self, registration):
            """返回无需工具执行的纯登记回执。

            Args:
                registration (DesktopWindowRegistration): 路由校验后的完整 pet/chat 原生窗口身份。
            """
            assert registration.windows.pet == "11" and registration.windows.chat == "12"
            return {"accepted": True, "window_restore_path": "isolated/restore.json"}

    app = FastAPI()
    app.include_router(router)
    scheduler = SimpleNamespace(phase=RuntimePhase.RUNNING)
    app.dependency_overrides[get_runtime_scheduler] = lambda: scheduler
    app.dependency_overrides[get_desktop_controller] = lambda: Desktop()
    install_local_api_security(app, "t" * 43)
    payload = {"owner_pid": 100, "revision": 1, "windows": {"pet": "11", "chat": "12"}}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/internal/desktop-windows", json=payload).status_code == 401
        client.headers["Authorization"] = "Bearer " + "t" * 43
        assert client.post("/internal/desktop-windows", json=payload).json()["accepted"]
        assert client.post("/internal/desktop-windows", json={**payload, "windows": {"settings": "11"}}).status_code == 422
        scheduler.phase = RuntimePhase.STOPPING
        assert client.post("/internal/desktop-windows", json=payload).status_code == 409
