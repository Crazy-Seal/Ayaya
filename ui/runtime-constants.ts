/** 运行时桌面策略的默认时长；用途不同的等待分别命名，不新增用户配置。 */

/** 聊天展示 SSE 的订阅重连间隔，单位为毫秒。 */
export const SUBSCRIPTION_RECONNECT_DELAY_MS = 1000;
/** 后台任务终态在聊天标题栏短暂展示的时间，单位为毫秒。 */
export const TASK_NOTICE_DURATION_MS = 4000;
/** 可见任务页刷新耗时与最近输出的间隔，单位为毫秒。 */
export const TASK_REFRESH_INTERVAL_MS = 2000;
/** 退出确认前查询后台任务状态的最长等待时间，单位为毫秒。 */
export const EXIT_TASK_QUERY_TIMEOUT_MS = 2000;
