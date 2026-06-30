- 该项目的后端需要在conda环境中运行，环境名为my_agent
- 该项目的代码注释和控制台打印信息应尽量使用中文书写

## 生产环境与测试环境切换

- 未设置 `AYAYA_ENV` 时默认使用生产环境，等价于 `AYAYA_ENV=production`。
- 生产环境使用项目下的 `memory/` 目录和 `config/chat_settings.yaml`，并加载项目根目录的 `.env`。
- 切换到生产环境：设置 `AYAYA_ENV=production`，并删除可能遗留的 `AYAYA_DATA_DIR`。
- 切换到测试环境：设置 `AYAYA_ENV=test`，同时必须把 `AYAYA_DATA_DIR` 设置为生产 `memory/` 目录之外的独立目录。
- 测试环境的 SQLite、checkpoint、Chroma、Mem0/Qdrant、图片和会话配置都必须位于 `AYAYA_DATA_DIR` 内；测试环境不会加载生产 `.env`。
- 自动化测试通过 `tests/conftest.py` 在导入后端模块前创建一次性测试目录，不得连接、写入或删除生产数据库。
- 在 PowerShell 中运行自动化测试：`conda run -n my_agent python -B -m pytest tests -q -p no:cacheprovider`。
- 手动启动测试后端前，需要在 `$env:AYAYA_DATA_DIR\config\chat_settings.yaml` 准备独立测试配置。
