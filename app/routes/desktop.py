"""桌面配置与窗口身份接口，全部沿用应用本地认证中间件。"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_desktop_settings_service, get_desktop_controller, get_runtime_scheduler
from app.schemas.desktop import DesktopSettings, DesktopWindowRegistration
from app.schemas.runtime_types import RuntimePhase
from app.desktop.desktop_controller import DesktopController
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.schemas.result import Result
from app.services.desktop_settings_service import DesktopSettingsService

router = APIRouter(tags=["desktop"])


@router.post("/internal/desktop-windows")
async def register_desktop_windows(
    registration: DesktopWindowRegistration,
    desktop: DesktopController = Depends(get_desktop_controller),
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """主进程登记两个明确 HWND，退出后拒绝登记而不启动输入。

    Args:
        registration (DesktopWindowRegistration): pet/chat 的完整身份快照。
        desktop (DesktopController): 共享桌面模块。
        scheduler (AgentRuntimeScheduler): 现有应用生命周期所有者。

    Returns:
        dict: 接受标记和已配置的恢复记录路径。
    """
    if scheduler.phase != RuntimePhase.RUNNING:
        raise HTTPException(409, "应用没有接受桌面登记")
    try:
        return await desktop.register_windows(registration)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/desktop_settings", response_model=Result)
def get_desktop_settings(service: DesktopSettingsService = Depends(get_desktop_settings_service)) -> Result:
    """设置页读取已保存 YAML，首次读取会完成旧截图开关的迁移。

    Args:
        service (DesktopSettingsService): 共享配置读写模块。

    Returns:
        Result: 已规范化并保存的配置。
    """
    try:
        return Result(data=service.get(), msg="success", code=200)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/desktop_settings", response_model=Result)
def update_desktop_settings(settings: DesktopSettings, service: DesktopSettingsService = Depends(get_desktop_settings_service)) -> Result:
    """保存完整桌面设置，退出期间拒绝新写入。

    Args:
        settings (DesktopSettings): 严格校验的布尔设置。
        service (DesktopSettingsService): 共享配置读写模块。

    Returns:
        Result: 实际写入 YAML 的配置。
    """
    try:
        return Result(data=service.update(settings), msg="success", code=200)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
