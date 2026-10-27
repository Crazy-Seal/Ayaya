"""验证独立插件包通过公共发现接口接入目录。"""

from pathlib import Path

from app.agent.plugins.registry import PluginRegistry


def test_external_package_loads_relative_imports(tmp_path: Path, monkeypatch) -> None:
    """独立包入口导出的类以稳定名称登记，支持工具文件的相对导入。"""
    monkeypatch.setattr(PluginRegistry, "_plugins", {})
    monkeypatch.setattr(PluginRegistry, "_lazy_plugins", {})
    package = tmp_path / "sample-package"
    package.mkdir()
    (package / "__init__.py").write_text("from .implementation import SamplePlugin\n", encoding="utf-8")
    (package / "implementation.py").write_text(
        "from app.agent.plugins.base import BasePlugin\n"
        "class SamplePlugin(BasePlugin):\n"
        "    name = 'sample_package'\n"
        "    hooks = []\n"
        "    async def execute(self, context): pass\n", encoding="utf-8",
    )
    assert PluginRegistry.discover_external(tmp_path) == []
    assert PluginRegistry.get("sample_package").name == "sample_package"
    assert not PluginRegistry.has("sample-package")


def test_broken_package_reports_error_without_overwriting(tmp_path: Path, monkeypatch) -> None:
    """包导入失败时报告目录错误，已有注册不受影响。"""
    package = tmp_path / "broken"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('包加载失败')\n", encoding="utf-8")
    before = PluginRegistry.list_plugins()
    errors = PluginRegistry.discover_external(tmp_path)
    assert len(errors) == 1
    assert "broken" in errors[0]
    assert PluginRegistry.list_plugins() == before


def test_duplicate_package_cannot_override_existing_plugin(tmp_path: Path) -> None:
    """独立包重用已有插件名称时报告错误并保留原类。"""
    original = PluginRegistry.get("memory")
    package = tmp_path / "duplicate"
    package.mkdir()
    (package / "__init__.py").write_text("from app.agent.plugins.memory import MemoryPlugin\n", encoding="utf-8")
    errors = PluginRegistry.discover_external(tmp_path)
    assert len(errors) == 1
    assert PluginRegistry.get("memory") is original
