"""提供桌面设置读写，沿用运行时退出门禁。"""

from collections.abc import Callable

from app.crud.desktop_settings_dao import DesktopSettingsDao
from app.schemas.desktop import DesktopSettings


class DesktopSettingsService:
    """协调配置访问和退出检查，不参与工具权限管理。"""

    def __init__(self, dao: DesktopSettingsDao, accepting: Callable[[], bool]) -> None:
        """保存配置 DAO 和当前应用是否接受操作的回调。

        Args:
            dao (DesktopSettingsDao): 当前环境的 YAML 原子存储。
            accepting (Callable[[], bool]): 现有 Scheduler 未进入退出状态时为 True。
        """
        self.dao = dao
        self._accepting = accepting

    def get(self) -> DesktopSettings:
        """应用可操作时读取或迁移配置，退出期间拒绝写入型首次初始化。

        Returns:
            DesktopSettings: 已确认保存在 YAML 中的全局配置。
        Raises:
            RuntimeError: 应用已进入退出门禁。
        """
        if not self._accepting():
            raise RuntimeError("应用正在退出")
        return self.dao.load()

    def update(self, settings: DesktopSettings) -> DesktopSettings:
        """退出门禁通过后原子保存，返回已保存配置。

        Args:
            settings (DesktopSettings): schema 已校验的完整配置。
        Returns:
            DesktopSettings: 原子替换成功后的配置。
        Raises:
            RuntimeError: 应用已进入退出门禁。
        """
        if not self._accepting():
            raise RuntimeError("应用正在退出")
        return self.dao.save(settings)
