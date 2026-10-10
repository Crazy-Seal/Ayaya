# UI (Electron + TypeScript)

桌宠前端位于 `ui/`，功能包括：
- 使用 Electron 创建透明无边框置顶窗口
- 窗口固定在桌面右下角
- 渲染 Live2D 角色
- 角色下方输入框通过桌面 Adapter 向 `POST /agent/commands` 提交消息，独立订阅会话事件
- 模型视线跟随鼠标移动
- 人物区域鼠标点击穿透（保留气泡/输入框/模型交互）
- 在对话框区域滚轮缩放控制桌宠大小

模型管理说明：
- 在设置窗口的“模型管理”中支持导入 Live2D 模型文件夹。
- 导入后模型会被复制到 `ui/user_data/live2d_models/{模型名}`。
- 模型与 `session_id` 映射会持久化在 `ui/user_data/models.json`。
- 删除模型时会同步删除 `ui/user_data/live2d_models/{模型名}` 目录和持久化记录。
- 默认内置模型不可删除。

## 窗口视觉架构

设置面板和日志控制台使用同一套轻量原生 CSS 设计系统：

- `src/window-theme.css` 只维护颜色、字体、间距、圆角、阴影、动画和标题栏等设计令牌。
- `src/window-components.css` 维护按钮、表单控件、复选框、滑块、卡片、表格、弹窗和反馈组件。
- `src/settings.css` 与 `src/logs/logs.css` 只维护页面布局、尺寸约束和业务展示样式。

新增窗口控件时应优先组合 `ui-*` 共享类。页面类可以调整宽度和位置，但不应重新定义同类控件的颜色、字号或 hover、active、focus、disabled 状态。危险操作统一使用 `ui-button--danger`，主要操作使用 `ui-button--primary`，辅助操作使用 `ui-button--secondary`。

说明：首次执行 `npm run dev` 或 `npm run build` 会自动下载 `live2dcubismcore.min.js` 到 `public/`，并自动解压模型资源。

## 安装

```powershell
cd ui
npm install
```

## 开发模式

```powershell
npm run dev
```

Electron 默认自动发现名为 `ayaya` 的 Conda 环境，并直接使用其中的 Python
启动受管后端。可通过 `AYAYA_PYTHON_EXECUTABLE` 显式覆盖解释器。

默认后端地址：`http://127.0.0.1:8000`。桌面事件使用 `/agent/events/{session_id}`，屏幕执行器使用独立的 `/desktop-executor/requests` 流。
命令、SSE 事件、执行器、后台任务和 Electron bridge 的字段与交付时序见[前后端接口文档](<../API Docs.md>)。

退出应用或后端重启会终止旧轮次，旧审批、桌面请求和未交付真人输入作废，不自动续跑工具。任务记录、输出和终态通知保留；遗留通知等待本次启动后新真人输入到达再交付。聊天事件重连仅恢复当前展示，不触发执行；同一后端进程内重连不会终止当前轮次。

运行时协议取值集中在 `shared-types.ts`，Renderer 与 Electron 共用具名常量和联合类型；重连、结果回传重试、任务通知、任务刷新和截屏隐藏等待的默认时长集中在 `runtime-constants.ts`。取值与默认行为保持不变，协议一致性测试使用后端测试目录中的固定 JSON 样例。

可通过环境变量修改：

```powershell
$env:AYAYA_BACKEND_BASE_URL="http://127.0.0.1:8000"
npm run dev
```

## 打包前构建

```powershell
npm run build
```

## 运行构建产物

```powershell
npm run start
```
