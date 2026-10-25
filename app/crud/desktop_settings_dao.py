"""原子读写桌面 YAML，首次导入旧前端设置但不修改窗口 JSON。"""

import json
import logging
import threading
from pathlib import Path

import yaml

from app.environment import get_desktop_settings_file, get_legacy_frontend_settings_file
from app.schemas.desktop import DesktopSettings

logger = logging.getLogger(__name__)


class DesktopSettingsDao:
    """串行读写全局桌面设置，配置修改从下一次操作开始生效。"""

    def __init__(self, config_file: Path | None = None, *, legacy_file: Path | None = None) -> None:
        """保存环境路径和写入锁；构造时不访问运行数据。

        Args:
            config_file (Path | None): 可注入的隔离 YAML 路径。
            legacy_file (Path | None): 首次迁移读取的旧 JSON 路径。
        """
        self.config_file = config_file or get_desktop_settings_file()
        self.legacy_file = legacy_file or get_legacy_frontend_settings_file()
        self._lock = threading.RLock()

    def load(self) -> DesktopSettings:
        """读取 YAML；文件缺失时导入有效旧值并先原子保存。

        Returns:
            DesktopSettings: 当前规范化配置，已存在的损坏 YAML 不会被默认值覆盖。
        """
        with self._lock:
            if self.config_file.exists():
                return DesktopSettings.model_validate(yaml.safe_load(self.config_file.read_text(encoding="utf-8")) or {})
            # 1. 只在 YAML 不存在时读取旧值，避免每次重连覆盖后端设置。
            value = True
            if self.legacy_file.exists():
                try:
                    legacy = json.loads(self.legacy_file.read_text(encoding="utf-8"))
                    if isinstance(legacy, dict) and type(legacy.get("hide_on_screenshot")) is bool:
                        value = legacy["hide_on_screenshot"]
                except (ValueError, OSError):
                    logger.warning("旧桌面配置无法读取，使用默认隐藏设置")
            return self.save(DesktopSettings(hide_on_screenshot=value))

    def save(self, settings: DesktopSettings) -> DesktopSettings:
        """写入同目录临时文件再替换，失败不覆盖原配置。

        Args:
            settings (DesktopSettings): 已校验的完整全局配置。

        Returns:
            DesktopSettings: 实际保存的配置。
        """
        with self._lock:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_file.with_suffix(".yaml.tmp")
            try:
                temporary.write_text(yaml.safe_dump(settings.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8")
                temporary.replace(self.config_file)
            finally:
                temporary.unlink(missing_ok=True)
            return settings
