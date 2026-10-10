/** 运行时桌面策略的默认时长；用途不同的等待分别命名，不新增用户配置。 */

/** 展示与执行 SSE 共用的订阅重连间隔，单位为毫秒。 */
export const SUBSCRIPTION_RECONNECT_DELAY_MS = 1000;
/** 同一屏幕执行结果回传失败后的重试间隔，单位为毫秒，不重做动作。 */
export const DESKTOP_RESULT_RETRY_DELAY_MS = 1000;
/** 截屏前等待桌面窗口隐藏生效的时间，单位为毫秒。 */
export const SCREEN_CAPTURE_HIDE_DELAY_MS = 120;
/** 后台任务终态在聊天标题栏短暂展示的时间，单位为毫秒。 */
export const TASK_NOTICE_DURATION_MS = 4000;
/** 可见任务页刷新耗时与最近输出的间隔，单位为毫秒。 */
export const TASK_REFRESH_INTERVAL_MS = 2000;
