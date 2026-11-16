# Windows 屏幕操作

当前 Python REPL 已绑定 `cua`。它在用户的活动桌面工作，真实键鼠会占用前台。
变量、窗口对象和导入可以跨 Python 调用保留；重置 REPL 后重新取得窗口对象。

## 发现和绑定窗口

```python
windows = cua.list_windows()
app = cua.get_app(window_id=windows[0].window_id)
state = app.get_ax_state_and_screenshot()
```

根据清单中的标题、程序名和 PID 选择准确 `window_id`，同名窗口也有各自编号。
`get_app` 只绑定该窗口。独立弹窗需要重新列举并绑定。
绑定不主动截图、读取控件或取得前台。

## 观察与输出

| 方法 | 返回值及输出 |
| --- | --- |
| `cua.list_windows()` | 窗口信息列表，同时打印准确编号、标题、PID 和可取得的程序名 |
| `cua.get_screenshot()` | 主屏幕 Screenshot，同时输出图片 |
| `app.get_ax_state()` | AxState，同时打印控件树、文本、焦点、状态及可用动作 |
| `app.get_screenshot()` | 该窗口的 Screenshot，同时输出图片 |
| `app.get_ax_state_and_screenshot()` | WindowState，同时输出控件信息和一张窗口图片 |

`WindowState.ax_state.nodes` 是控件列表，`WindowState.screenshot` 是图片结果。
节点包含 `element_index`、`parent_index`、`name`、`role`、`bounds`、`enabled`、
`focused`、`text` 和 `actions`。密码控件不输出值。

`Screenshot.image` 是完整 PNG bytes；`width/height` 是输出图片尺寸，`region`
记录原始物理范围和 DPI。观察方法已经输出所请求内容，无需再 print 或 display_image。
普通 Python 图片使用原生 `display_image(PIL图片或PNG/JPEG bytes)` 输出。

## 选择操作方式

针对当前任务，优先使用控件支持的语义动作；需要点击时优先按控件定位；缺少可用控件时，
再使用最新截图中的坐标。这是选择建议，每次只执行所需的动作。

```python
app.set_value(element_index=12, value="完整文本")
app.perform_secondary_action(element_index=15, action="Invoke")
app.click(element_index=18)
```

`set_value` 要求可写 ValuePattern。`perform_secondary_action` 支持 `Invoke`、
`Toggle`、`Select`、`Expand`、`Collapse`，只使用控件实际列出的动作。
观察中的 `SetValue` 对应 `set_value` 方法。

控件编号属于最近一次 AX 观察。重新读取树后旧编号失效，不要猜编号或跨窗口复用编号。
控件消失、不可操作或不支持动作时方法会报错。结果不明时先观察，方法不会自动换方式重试。

## 坐标操作

```python
app.get_screenshot()
app.click(x=500, y=500)
app.click(x=500, y=500, button="right")
app.click(x=500, y=500, clicks=2)
```

`x/y` 都是 `0～1000` 的整数，表示最近截图内部的位置。
窗口对象使用其最近窗口截图，`cua.click` 使用最近一次 `cua.get_screenshot()` 的主屏画面。
环境自动注入的画面不建立坐标映射。

窗口截图包括实际捕获的标题栏和边框，插件负责换算桌面物理坐标。
仅移动窗口时自动更新原点；尺寸或 DPI 改变后必须重新截图。1000 对应区域最末侧有效像素。
界面布局变化后也应主动重新观察。截图及操作保持桌宠和聊天窗当前显隐。

控件编号与 `x/y` 只能选一种。真实窗口输入前会激活并核对目标，遮挡或失效会报错。
`cua` 级操作面向整个主屏幕，适用于任务栏、桌面图标和其他全局目标。

## 文字、按键、滚动和拖动

窗口对象与 `cua` 都提供以下方法；窗口对象会先激活准确目标，`cua` 保持当前前台目标。

```python
app.hotkey("ctrl", "a")
app.type_text("中文内容")
app.press_key("enter")
app.scroll(element_index=12, direction="down", amount=3)
app.scroll(x=500, y=700, direction="up", amount=2)
app.drag(start_x=200, start_y=300, end_x=700, end_y=300)
```

`press_key` 接受单键名称，`hotkey` 按顺序按下并释放组合键。常用名称包含 ctrl、shift、alt、
win、enter、tab、esc、space、backspace、delete、insert、home、end、方向键、pageup、pagedown、F1～F24。
普通字母、数字和可映射的单字符也可作为按键。完整文本使用 `type_text`。

`type_text` 使用可恢复的剪贴板粘贴，不自动追加回车。当前剪贴板含文件或无法可靠恢复的格式时，
输入会明确失败并保留用户内容；输入期间用户复制的新内容优先保留。

`scroll` 的方向为 up/down，amount 为正整数滚轮格数。窗口对象可选控件或坐标，
`cua.scroll` 必须提供主屏截图坐标。拖动起终点来自同一张最近截图，结束时释放左键。

## 动作结果与停止

动作返回并打印执行回执，操作后请按需要主动观察，不会自动附带新截图。
首次改变桌面状态后，聊天页显示“正在控制屏幕，按 ESC 退出”，持续到整次 Python 调用结束，
中途计算或 sleep 不会反复切换提示。

用户物理 ESC 终止本次代码。模型发送的 `press_key("esc")` 仍作为目标应用输入。
收到用户终止结果后，停止后续操作并结束本轮。
聊天停止按钮由现有流程结束 Agent；两种停止都会保留 runner 和已经完成的变量修改。
系统原生调用正在阻塞时允许等它返回再处理停止，原生 REPL 的总执行超时继续适用。

调用结束会解除监听、释放本次输入并关闭提示。已经发生的桌面修改不会回滚。
