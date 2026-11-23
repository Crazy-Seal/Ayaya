# Ayaya 项目词表

本词表统一需求说明、代码注释、接口文档和技术讨论中的项目用语。范围包括后端运行时、Agent、子 Agent、工具、插件、记忆、模型上下文和桌面操作。

本文定义当前代码中的概念。术语调整只统一表达，业务行为的改变应另行说明。

## 使用规则

1. 涉及本文定义的概念时，使用“标准术语”列中的名称。禁止为同一概念自行换词、缩写或创造别名。例如，统一写“主会话”“子 Agent”“执行轮次”。
2. 可以加上必要的对象或时间限定，例如“主 Agent 的执行轮次”“上次启动时未完成的工具调用”。限定信息应说明当前功能所需的对象、条件或结果。
3. 核心组件直接使用原类名，例如 `ParentSession`、`AgentExecution`、`TaskSupervisor`。不为类名另设中文名称。代码标识符保持原样。
4. 类、函数和方法的文档注释说明用途：谁在什么情况下使用它、处理什么、产生什么结果。参数、返回值和异常说明补充调用者需要知道的含义。锁、事务、队列操作、重试顺序等实现说明写在对应代码旁。
5. 注释应在当前定义附近读得懂。使用术语时仍需补齐对象，例如“加载当前 Agent 对话的 checkpoint”，而非“加载状态”。
6. 涉及多个概念时分别写明。例如，“保存 checkpoint 和对话历史”“提供应用启动批次编号和该会话的最晚已发布事件序号”，不将它们压缩成一个抽象名称。
7. 本文未覆盖的项目专用概念，先补充标准术语、定义和代码对应关系，再在新注释中使用。普通中文动词和通用编程术语按正常含义使用。
8. 修改术语时在本文统一修改，并检查相关注释和文档。历史 ADR 中记录旧实现的文字保留其历史含义。

## 一、Agent、对话和委派

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| Agent | 根据模型输出生成回答、调用工具并处理工具结果的程序对象。 | `Agent`。本文统一使用 Agent，不交替使用“智能体”“代理”。 |
| 主 Agent | 直接处理用户消息的 Agent，可以创建子 Agent 并向子 Agent 分配工作。 | `build_agent()` 中未指定 `subagent` 的 Agent。“父 Agent”统一写为“主 Agent”。 |
| 子 Agent | 主 Agent 创建的受派 Agent，使用独立的对话上下文处理分配给它的工作。 | `SubagentDefinition` 及其对应的 Agent。当前仅支持主 Agent 与子 Agent 之间通信。 |
| 主会话 | 用户与 Ayaya 长期交互的一段对话，也是会话配置和所属子 Agent 的管理范围。 | `ChatSettings.session_id`、`ParentSession`。不写“父会话”“父上下文”。 |
| 子 Agent 对话 | 某个子 Agent 自己的对话，保存它收到的指令、模型回答、工具调用和工具结果。 | 使用 `subagent_id` 区分；数据保存在 `AgentStorageScope.SUBAGENT` 对应的表中。 |
| Agent 对话 | 主会话和子 Agent 对话的统称。用于同时适用于两者的功能说明。 | `AgentState.session_id`、`AgentExecution.context_id` 对应的对话。 |
| 会话配置 | 一个主会话的模型、系统提示词、角色描述、工具权限和插件设置。 | `ChatSettings`。描述具体字段时写“模型配置”“工具权限”等具体名称。 |
| 子 Agent 定义 | 创建子 Agent 时保存的名称、系统提示词、工具白名单、保留方式和所属主会话。 | `SubagentDefinition`。 |
| 子 Agent 记录 | 子 Agent 定义及其废弃时间。用来判断子 Agent 是否仍可接受操作。 | `SubagentRecord`。 |
| 子 Agent 状态摘要 | 提供给主 Agent 或界面的子 Agent 名称、运行状态、审批等待情况和最近错误等信息。 | `SubagentSummary`。 |
| 委派任务 | 主 Agent 交给子 Agent 的一次工作，从受理开始，到完成、失败或停止结束。期间可以有多个执行轮次。 | `SubagentSession` 管理的一次工作，结果记录使用 `TaskRecordKind.SUBAGENT`。 |
| 主 Agent 指令 | 主 Agent 发给子 Agent、要求开始任务、调整工作或回复问题的一条消息。 | `InboxKind.AGENT_COMMAND`；用户直接向子 Agent 发送的管理消息也经此输入类别处理。 |
| 子 Agent 消息 | 子 Agent 通过通信工具发给主 Agent 的内容，例如问题或进展说明。 | `ParentMessageRequest`、`InboxKind.SUBAGENT`、`SubagentMessage`；需要回复时也可随等待请求保存。 |
| 子 Agent 最终报告 | 委派任务结束后交给主 Agent 的结果说明。 | `SubagentService.finish_run()`、`SubagentInteractionKind.FINAL`。与过程中的子 Agent 消息分别描述。 |
| 一次性子 Agent | 委派任务结束后自动废弃、后续无法再次分配任务的子 Agent。历史记录继续保留。 | `SubagentDefinition.if_retain_context=False`。 |
| 保留型子 Agent | 委派任务结束后保留对话上下文、可以再次接受委派任务的子 Agent。 | `SubagentDefinition.if_retain_context=True`。应用重启后等待新的明确操作。 |
| 工具白名单 | 主 Agent 为子 Agent 选择的工具范围，实际可用工具还受主会话工具权限和工具使用范围限制。 | `SubagentDefinition.tool_whitelist`。 |
| 仅主 Agent 可用的工具 | 只向主 Agent 提供的工具。 | `BaseTool.parent_only=True`。不写“父侧工具”。 |
| 废弃子 Agent | 保存子 Agent 的废弃时间并删除它尚未交付的输入，使其后续无法接受任务和消息。已保存的对话和结果继续保留。 | `SubagentDao.retire()`。销毁子 Agent、一次性子 Agent 结束、删除主会话及启动整理时会使用此操作。 |
| 销毁子 Agent | 用户或主 Agent 要求结束并移除指定子 Agent 的完整操作，包括废弃子 Agent、停止当前执行、停止所属后台任务和关闭资源。 | `SubagentAction.DESTROY`、`SubagentSession.destroy()`。废弃保存成功后返回受理结果，资源关闭随后进行。 |

`HumanMessage` 是输入消息的代码类型。当前代码也会将主 Agent 指令转换成 `HumanMessage`，因此仅凭这个类型不能判定消息来自用户。用户直接发给子 Agent 的内容仍称“用户消息”。注释应根据实际入口写“用户消息”或“主 Agent 指令”；解释类型本身时写 `HumanMessage`。

## 二、执行过程和控制操作

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| 后端运行时 | 后端管理 Agent 对话、执行轮次、后台任务和界面事件的运行部分。 | `app/agent/runtime/` 及应用为其提供的共享服务。 |
| 本次启动 | 从本次启动后端到后端退出的时间范围。 | 表达运行对象、事件编号和后台任务的本次运行范围。 |
| 上次启动 | 最近一次已经结束的后端运行。 | 表达应用重启后发现的未完成执行、工具调用和任务记录。 |
| 执行轮次 | Agent 开始处理一批输入后，到正常完成、停止、引导结束或失败的一次执行。等待审批和等待主 Agent 回复期间仍属于原执行轮次。 | `run_id`、`AgentExecution`。不将一次模型请求称为执行轮次。 |
| 模型请求 | 向模型发送一次模型输入并接收其回答或工具调用要求。 | `LLMClient` 的一次调用。一个执行轮次可以包含多次模型请求。 |
| 执行步骤 | 一次模型请求得到的助手消息，以及该消息要求执行的工具调用。 | `AgentState.record_assistant_step()`、`AssistantMessage.tool_calls`。 |
| 引导 | 保存新指令，结束当前回答，并根据新指令继续工作。 | `HUMAN_STEER`、`request_steer()`；子 Agent 的引导仍属于原委派任务。 |
| 停止当前执行轮次 | 请求结束 Agent 正在处理的执行轮次，并保存已经生成的回答及未完成工具调用的结束结果。 | `STOP_TURN`、`request_stop()`。会话是否暂停由对应入口决定。 |
| 停止委派任务 | 结束子 Agent 当前的委派任务。保留型子 Agent 可以在之后接受新任务；一次性子 Agent 按结束规则废弃。 | 子 Agent 的 `SubagentAction.STOP`、`SubagentSession.stop_current()`。 |
| 停止后台任务 | 请求结束某项由 `TaskSupervisor` 管理的独立工作。 | `TaskSupervisor.stop_task()`。明确写出目标任务。 |
| 等待用户审批 | 工具执行前已保存审批请求，等待用户批准、拒绝或发送引导消息。 | `ApprovalWait`、`InterruptKind.AUTHORIZATION_REQUEST`。 |
| 等待主 Agent 回复 | 子 Agent 已向主 Agent 发送消息并暂停当前执行，等待主 Agent 的下一条指令。 | `ParentMessageWait`、`InterruptKind.WAIT_FOR_PARENT`。 |
| 审批恢复 | 审批决定保存后继续原执行轮次，并处理等待中的工具调用。 | `AgentExecution.resume()`。审批框引导另按引导规则处理。 |
| 启动会话 | 允许会话处理待处理输入，或在后续输入到达时继续工作。 | `SessionState.ENABLED`。有可处理输入时才开始执行；这里不表示启动后端或创建会话。 |
| 暂停会话 | 暂停自动处理输入，已收到的任务结果通知和插件通知继续保存，等待后续明确操作启动会话。 | `SessionState.PAUSED`。等待用户审批和暂停会话分别描述。 |
| 关闭会话 | 停止接受新的会话操作，并结束当前工作、释放该会话使用的资源。 | `SessionState.CLOSED`、`begin_close()`、`close()`。 |
| 启动整理 | 后端启动时处理上次启动留下的待处理输入、未完成工具调用和任务记录。 | `AgentRuntimeScheduler.start()` 中的整理操作。 |
| 执行收尾 | 执行结束时保存回答、补充未完成工具调用的结果并释放相关资源。 | 写出场景，例如“停止后的执行收尾”“失败后的执行收尾”。 |
| Agent 可用性检查 | 检查主会话配置是否存在；涉及子 Agent 时，继续检查子 Agent 是否存在、所属主会话是否匹配、是否已废弃。 | `AgentIdentityService`。不写“身份资格检查”。 |
| 调用来源检查 | 检查工具请求来自哪个主会话、子 Agent、执行轮次和工具调用，并确认该执行轮次仍有效。 | `SubagentSource`、`AgentRuntimeScheduler._check_source()`。 |

“进程”仅在讨论真实操作系统进程时使用，例如“终端进程”“Python 执行进程”“后端进程退出码”。描述应用重启前后的工作时使用“本次启动”“上次启动”。

## 三、输入、命令和通知

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| 用户消息 | 用户通过界面或交互入口发送的文字、图片等内容。 | 主会话输入的 `InboxKind.HUMAN`。不写“真人意图”“Human 消息”代替来源说明。 |
| 消息 | Agent 对话中的一条内容记录，按来源和用途区分用户消息、助手消息、工具结果消息及通知等类型。 | `BaseMessage` 及其子类。不写“领域消息”“协议消息”代替具体类型。 |
| 助手消息 | 一次模型响应中 Agent 给出的文字内容和工具调用要求。 | `AssistantMessage`。 |
| 工具结果消息 | 加入 Agent 对话的一次工具调用结果，包含对应的工具调用编号。 | `ToolMessage`。工具执行刚返回的结果对象仍称“工具结果”。 |
| 系统消息 | 向模型说明 Agent 的角色、职责和执行要求等内容的消息。 | `SystemMessage`、系统提示词。 |
| 消息内容块 | 一条消息中的一段文字或一张图片。 | `ContentPart`、`TextContent`、`ImageContent`。 |
| 输入 | 交给 Agent 处理的用户消息、主 Agent 指令、子 Agent 消息、任务结果通知或插件通知。 | `InboxKind` 区分种类，`InboxEnvelope` 表示 SessionInbox 中的一项输入，附带编号、交付状态和排队方式。 |
| SessionInbox | 保存等待 Agent 处理的输入，记录每项输入的交付状态和排队方式。 | 当前由 `SessionInboxDao` 访问 `session_inbox`、`subagent_session_inbox`。SessionInbox 是本文使用的概念名称。 |
| 待处理输入 | 已保存到 SessionInbox、尚未交付给 Agent 的输入。 | `InboxStatus.PENDING`。不写“持久输入”“待领取材料”。 |
| 排队消息 | 用户要求等当前执行轮次正常结束后再处理的消息。 | `QUEUE_TURN`、`InboxDeliveryMode.QUEUED`。 |
| 放行排队消息 | 当前执行轮次正常结束后，将对应排队消息设为可以参加后续输入交付。 | `queue_ready`、`release_queued_in_transaction()`。 |
| 交互命令 | 用户通过交互入口提出的操作，例如发送消息、停止执行或提交审批决定。 | `InteractionCommand`、`CommandKind`。 |
| 受理 | 入口已完成该请求要求的输入保存、审批决定保存或控制请求接收。 | `CommandReceipt`。具体注释写清本次受理完成了哪项操作。 |
| 命令受理结果 | 一次交互命令的受理状态、目标编号及失败说明。 | `CommandReceipt`、`CommandStatus`。不把受理成功写成任务完成。 |
| 交付输入 | 将输入加入 Agent 状态，并保存 checkpoint 和输入的已交付标记，使后续执行可以使用该内容。 | `InboxStatus.DELIVERED`、`SessionStateManager` 的输入保存操作。 |
| 插件通知 | 插件主动发送给 Agent、需要保存到对话中的内容，例如检测到环境变化。 | `PluginRequestKind.NOTIFY`、`PluginNotificationMessage`。 |
| 任务结果通知 | 任务结束后提供给接收 Agent 的状态和结果内容，是否发送由任务配置决定。 | `InboxKind.TASK_TERMINAL`、`TaskResultMessage`、`TaskResultBatchMessage`。 |
| 短暂界面提示 | 插件请求界面短时间显示的提示文字。 | `InteractionEventKind.NOTICE`。 |
| 持续界面提示 | 插件请求界面保持显示、直到更新或关闭的状态提示。 | `InteractionEventKind.STATUS_NOTICE`。 |
| 执行结果 | Agent 一段执行结束或进入等待时返回给 `ParentSession` 或 `SubagentSession` 的结果。 | `AgentRunOutcome`。它与命令受理结果分别表示不同阶段的结果。 |

## 四、Agent 状态、checkpoint 和对话历史

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| Agent 状态 | 当前 Agent 对话使用的消息、未完成工具调用、等待信息、临时上下文和插件状态。 | `AgentState`。它位于内存中，调用保存操作后才写入数据库。 |
| 对话上下文 | 为 Agent 的后续模型请求保留的对话内容及相关背景。可包含压缩后的历史内容。 | 使用本词时明确是在描述 Agent 保留的内容，还是本次模型输入。 |
| checkpoint | 为某个 Agent 对话保存的一份 Agent 状态，用于后续加载该对话和读取已保存的执行进度。 | `AgentState.to_checkpoint()`、`AgentStateDao`。统一写 checkpoint，类型直接写 `INTERMEDIATE` 和 `COMPLETED`。 |
| INTERMEDIATE | Agent 在处理输入、执行工具或进入等待时保存的 checkpoint 类型。 | `CheckpointType.INTERMEDIATE`。 |
| COMPLETED | Agent 的执行轮次正常完成，或完成停止、引导、失败收尾后保存的 checkpoint 类型。 | `CheckpointType.COMPLETED`。 |
| 对话历史 | 已保存的用户消息、Agent 回答、工具调用、工具结果和通知等对话记录，供历史查询与记忆处理使用。 | `conversation_messages`、`subagent_conversation_messages`。 |
| 历史消息截止编号 | 用消息编号指定历史查询应包含到哪条消息。界面恢复历史时可用它限制查询范围。 | `through_message_id`，对应历史表中的字符串 `message_id`。 |
| 历史记录截止编号 | 用数据库记录编号指定历史查询应包含到哪条记录。记忆任务用它排除任务准备之后新增的消息。 | `max_history_id`，对应同一历史表中的整数 `id`；由 `get_protocol_history_cutoff()` 等操作取得。 |
| 临时上下文 | 本执行轮次提供给模型参考的背景信息，例如相关记忆、子 Agent 名单或桌面信息。 | `AgentState.temporary_context`；其中每项 `TemporaryContextItem` 包含标题、正文和图片，随 checkpoint 保存。 |
| 环境感知消息 | 每次模型请求前，将当前临时上下文整理成的一条消息，供本次模型请求读取。 | `EnvironmentPerceptionMessage`。按当前临时上下文重新生成，只加入本次模型输入。 |
| 保存故障 | Agent 执行所必需的内容保存失败，当前执行停止并报告错误。 | `RequiredSaveError`、`ExecutionPhase.FAULTED`、`AgentRunOutcomeKind.SAVE_FAILED`。 |
| 恢复已保存状态 | 保存故障后重新加载最后成功保存的 checkpoint 和历史消息截止编号，使后续查询和明确输入使用已保存的内容。 | `AgentExecution.restore_committed_state()`、`HISTORY_RESTORED`。 |
| 任务结果保存失败 | 任务业务已经结束，但最终结果保存失败；本次启动期间的任务查询提供业务结果和保存错误。 | `TaskExecutionPhase.SAVE_FAILED`、`TaskSnapshot.save_error`。 |
| 最终报告交付失败 | 子 Agent 对话已经保存，但最终报告未能保存到主会话的 SessionInbox。 | 子 Agent 最终报告保存与交付流程。 |

checkpoint 保存 Agent 后续执行要加载的状态，对话历史保留对话记录。上下文压缩可能改变 checkpoint 中的消息内容，对话历史仍保留原始内容。两者应分别命名。

同一条历史记录可以同时有整数 `id=104` 和字符串 `message_id="msg-a"`。历史记录截止编号使用 `104`，历史消息截止编号使用 `"msg-a"`。它们都限制历史查询的范围，使用的编号不同。

临时上下文随 checkpoint 保留。每次请求模型时，根据当前临时上下文生成环境感知消息；环境感知消息本身不另存为原始对话历史。

## 五、运行阶段和界面状态

| 标准术语 | 定义 | 对应代码 |
| --- | --- | --- |
| 后端运行阶段 | 后端当前处于初始化、运行、关闭中还是已停止。 | `RuntimePhase`。 |
| 会话状态 | 主会话或子 Agent 对话当前允许处理输入、暂停处理输入还是已经关闭。 | `SessionState`。 |
| Agent 执行阶段 | 某个 Agent 当前处于空闲、运行、等待、停止收尾、最终保存还是保存故障阶段。 | `ExecutionPhase`。 |
| 会话展示状态 | 告诉界面当前应显示空闲、运行中还是等待审批，用来决定发送、停止、排队按钮等交互行为。 | `SessionPhase`，由 `AgentExecution.presentation()` 根据 Agent 执行阶段和审批等待信息生成。 |
| 任务执行阶段 | 某个 `TaskSupervisor` 管理的任务当前处于运行、停止中、结果保存、已结束还是结果保存失败阶段。 | `TaskExecutionPhase`。 |
| 任务状态 | 描述任务当前是运行中、已完成、失败、已停止还是孤儿任务，供任务查询、界面显示和启动整理使用。 | `TaskStatus`，保存在任务记录中。 |
| 任务结果 | 交付给 Agent 的任务结果消息中的成功、失败、取消、孤儿任务或混合结果分类。 | `TaskResult`。`STOPPED` 转为 `CANCELLED`；多项结果合并后分类不一致时使用 `MIXED`。 |
| 子 Agent 展示状态 | 主 Agent 名单中的空闲、运行中或等待主 Agent 回复状态。界面另有是否等待用户审批的字段。 | `SubagentPublicStatus`、`SubagentSummary.waiting_for_approval`。 |

以上各项不能统称为一个“状态”后随意替换。例如，会话状态为暂停时，任务记录仍可能显示运行中；会话展示状态为空闲时，Agent 执行阶段也可能是保存故障。

## 六、队列、事件和状态快照

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| 运行时请求队列 | 后端各组件向 `AgentRuntimeScheduler` 发送请求和事件的共享内存队列。 | `runtime_queue`、`RuntimeRequest`。其中也包含任务记录和执行事件。 |
| 运行时请求队列处理循环 | 持续读取运行时请求队列，处理工具请求、插件请求、任务通知及 Agent 执行期间事件，并发布界面事件。 | `AgentRuntimeScheduler._receive_runtime_requests()`、`_task_reader`。每次后端启动创建一个。 |
| 会话处理循环 | 为一个 Agent 对话选择下一批输入，开始或恢复 Agent 执行，并在执行结束后处理继续工作、暂停和关闭。没有可处理工作时等待会话唤醒信号。 | `ParentSession._run_worker()`、`SubagentSession._run_worker()`、`_worker`。每个 Agent 对话分别安排。 |
| 会话唤醒信号 | 提醒会话处理循环结束等待，重新检查输入和会话状态的信号。 | `_wakeup`。 |
| Agent 执行期间事件 | Agent 执行期间产生的输入已保存、正文分片、工具调用、等待或完成等事件。 | `AgentEvent`、`EventType`；`ExecutionEvent` 补充所属 Agent 对话编号和执行轮次编号。 |
| 界面事件 | 后端提供给界面的消息、状态和提示更新。 | `InteractionEvent`、`InteractionEventKind`。不与 Agent 执行期间事件混称。 |
| 事件订阅 | 界面建立的事件接收连接及其对应的事件缓冲。 | `InteractionSubscription`、`GET /agent/events`。 |
| 应用启动批次编号 | 区分后端应用本次启动和此前启动所发布的界面事件。 | `stream_epoch`。当前在应用创建 `InteractionEventHub` 时生成，本次启动共用。 |
| 会话事件序号 | 同一事件流批次内，每个主会话分别递增的已发布界面事件编号。 | `event_sequence`、`InteractionEventHub._sequences`。 |
| 最晚已发布事件序号 | 某个主会话在当前应用启动批次内，最后已发布的界面事件序号。 | `InteractionEventHub._sequences[session_id]`，也是 `watermark(session_id)` 返回的第二项。 |
| Agent 执行状态快照 | 查询某个 Agent 当前的执行轮次、执行阶段、等待请求和保存错误，供会话管理及状态查询使用。 | `ExecutionSnapshot`。只描述该 Agent 当前的执行情况。 |
| 会话状态快照 | 页面加载、切换主会话或重连时，用来恢复界面的会话状态、执行情况、审批提示、排队消息、任务及子 Agent 信息。 | `SESSION_SNAPSHOT`、`AgentRuntimeScheduler.snapshot()`；其中包含 Agent 执行状态快照，并附带应用启动批次编号及该会话的最晚已发布事件序号。 |
| 任务状态快照 | 查询某项任务的运行进展和结果，包含任务记录、任务执行阶段及结果保存错误。任务列表、等待任务结束等操作使用它。 | `TaskSnapshot`。业务已结束但结果保存失败时，可补充数据库记录中尚未保存的信息。 |
| `ApprovalQueue` | 按主会话保存待展示审批的先后顺序，每项指明实际需要审批的 Agent 对话和请求编号。 | `ApprovalQueue`。 |

“checkpoint”是保存到数据库的 Agent 状态；“状态快照”是供查询或界面展示的数据。注释应写出具体名称，例如“会话状态快照”，不能单用“快照”同时指代两者。

`watermark(session_id)` 返回两项：应用启动批次编号、该会话的最晚已发布事件序号。最晚已发布事件序号只指其中的数字，不能用它指代这两个值的组合。

## 七、工具、审批、插件和后台任务

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| 工具 | Agent 可以请求执行的具体操作，例如读取文件、运行命令或查询记忆。 | `BaseTool` 及其实现。 |
| 工具定义 | 提供给模型的工具名称、说明和参数结构。 | `ToolDefinition`。 |
| 工具标识 | 由工具来源、名称、所属插件和可选 MCP 服务编号确定的工具名称组合，用来区分同名工具。 | `ToolIdentity`、`stable_key`。不写“工具身份”。 |
| 工具调用 | 模型要求执行某个工具及其参数的一次请求。 | `ToolCall`、`ToolInvocation`。 |
| 工具参数 | 一次工具调用提供的具体参数值，例如文件路径或命令正文。 | `ToolCall.args`、`ToolInvocation.arguments`。 |
| 工具结果 | 一次工具调用返回的文字、图片和其他结果内容。 | `ToolResult`；加入对话后使用 `ToolMessage` 保存。 |
| 工具调用与结果配对 | 对话中的每个工具调用都应有对应工具结果；停止或启动整理时，也要为未完成调用记录明确结果。 | `tool_call_id`、`pending_tool_calls`、`close_unfinished_tools()`。不写“补齐协议”“闭合工具协议”。 |
| 工具权限 | 主会话对某工具设置的禁止、需要审批或允许执行规则。 | `ToolPolicy.DENY`、`ASK`、`ALLOW`。 |
| 审批请求 | 等待用户决定是否执行某次工具调用的请求。 | `InterruptEvent` 中类型为 `AUTHORIZATION_REQUEST` 的内容。 |
| 审批决定 | 用户对某个审批请求提交的批准、拒绝或引导。 | `ApprovalDecisionKind`。 |
| 取消审批 | 新输入、停止或关闭等操作使待处理审批失效，由后端结束该审批请求。 | `AuthorizationResolutionKind.CANCELLED`。用户可以提交的审批决定仍为批准、拒绝或引导。 |
| 单次工具授权 | 用户批准后保存的记录，限定本次工具调用、参数和已经准备的操作。 | `AuthorizationGrant`。 |
| 工具定义校验指纹 | 根据工具名称、说明、参数结构等生成的校验值，用来判断审批前后工具定义是否变化。 | `contract_fingerprint`、`tool_contract_fingerprint()`。 |
| 已准备操作 | 工具在审批前整理出的具体待执行动作，用于向用户说明并确定本次批准的操作。 | `prepared_action`。 |
| 工具恢复数据 | 工具进入等待前保存的中间数据，继续执行时用于找到原来的操作进度。 | `resume_state`。 |
| 插件 | 为应用增加一组功能的扩展，可以提供工具、MCP 服务、插件钩子、后台任务、通知和配置。 | `BasePlugin`。 |
| 插件钩子 | 在 Agent 执行的某个约定时机自动调用插件功能的入口，例如请求模型前检索记忆。 | `PluginHook`、`BasePlugin.execute()`。 |
| 随应用启动的插件任务声明 | 插件说明应用启动或插件启用时需要创建哪些后台任务的定义。 | `PluginBootTask`、`on_application_boot_tasks()`。 |
| 随应用启动的插件任务 | 根据随应用启动的插件任务声明，由应用创建并交给 `TaskSupervisor` 执行的后台任务。 | `PluginBootTaskManager`。 |
| 后台任务 | 由应用继续执行和保存结果、可以跨执行轮次运行的独立工作。 | `TaskSupervisor` 管理的后台工作。不是所有后台任务都运行终端进程。 |
| 静默后台任务 | 结束时不向 Agent 发送任务结果通知的后台任务，状态和错误仍可查询。 | `notify_agent=False`。 |
| 孤儿任务 | 上次启动时记录为运行中，应用重启后已失去原执行对象、无法继续确认原执行结果的任务。 | `TaskStatus.ORPHANED`。 |
| Skill | 完成一件事的说明书，可以附带相关材料。 | 需求文档中的概念；当前不将其作为插件注册类型。 |

`ApprovalDecisionKind` 表示用户提交的批准、拒绝或引导。`AuthorizationResolutionKind` 在这三项之外还包含后端产生的 `CANCELLED`，供界面结束已失效的审批提示。本文不再另外定义“审批处理结果”这个术语。

`TaskRecordKind.ORDINARY` 是任务记录的内部分类，覆盖终端任务、记忆提取任务等由 `TaskSupervisor` 管理的任务；`TaskRecordKind.SUBAGENT` 用于委派任务的结果记录。本文不把“普通任务”作为额外的需求术语。

## 八、模型输入、记忆和桌面功能

| 标准术语 | 定义 | 对应代码或使用范围 |
| --- | --- | --- |
| 模型输入 | 本次模型请求实际使用的系统提示词、消息、临时上下文、图片和工具定义等内容。 | `PreparedModelInput` 及 `ModelAdapter` 整理的请求。 |
| `ModelAdapter` | 将项目消息和工具定义转换为指定模型接口格式，并将模型响应转换回项目格式。 | `ModelAdapter`、`OpenAIChatCompletionsAdapter`。 |
| 模型供应商数据 | 模型接口返回、后续请求可能需要回传的专用信息。 | `ProviderData`。使用时写明用途，例如继续原模型的推理过程。 |
| 上下文压缩 | 为控制后续模型输入的长度，缩短旧工具结果或用对话摘要替换较早的对话内容。 | `ContextCompactionCoordinator`、`ContextCompressionPolicy`。 |
| 对话摘要 | 从较早对话中整理出的主题、约束、决定和未完成事项等内容，供后续模型请求参考。 | `ContextSummary`、`ContextSummaryMessage`。 |
| 模型输入 token 估算 | 在请求模型前估计本次输入长度，用于判断是否需要压缩内容。 | `TokenAssessment` 及输入预算相关代码。 |
| 模型输入 token 校准记录 | 保存最近一次模型报告的实际输入 token 数和本地估算值，供后续估算调整使用。 | `TokenBaseline`。不写“真实输入锚点”。 |
| 长期记忆 | 从过去对话中整理并单独保存、可在后续对话中检索使用的信息。 | 记忆插件的长期记忆提取包括语义记忆和情景记忆；日记使用单独的名称。 |
| 语义记忆 | 从对话中整理的事实、偏好和知识等信息。 | `Mem0SemanticMemory`。 |
| 情景记忆 | 从对话中整理的事件或关键互动，包含事件内容和发生时间等信息。 | `EpisodicMemory`。 |
| 日记 | 根据某天的对话整理并按日期保存的内容。 | `DiaryMemory`。 |
| 记忆提取 | 从指定对话历史中整理并保存语义记忆或情景记忆。 | 长期记忆提取任务和各记忆实现的 `add()`。 |
| 记忆检索 | 按当前消息、查询内容或日期查找相关记忆和日记。 | 记忆插件的自动检索及记忆查询工具。统一用“记忆检索”，不与“召回记忆”交替使用。 |
| 记忆输入计数 | 记忆插件累计的用户消息数量，用来判断是否达到配置的长期记忆提取间隔。 | `MemoryState.long_term_memory_turn_counter`。代码字段中的 `turn` 在这里表示用户输入计数。 |
| 记忆操作 | 一次记忆查询、记忆提取或日记处理，使用准备该操作时取得的配置。 | `MemoryOperation`。 |
| 记忆操作配置 | 为一次记忆操作保留的模型配置、记忆功能开关和存储配置。 | `MemoryOperation.settings`、`config`、`options`。不笼统写“材料”。 |
| Python REPL | Agent 多次运行 Python 代码时共用的执行环境，保留已定义变量和已导入模块，直到重置或执行进程重启。 | `ReplService`、Python 执行进程。 |
| REPL 调用信息 | 一次 Python 工具调用所属的主会话、执行轮次、工具调用和可用插件能力。 | `ReplCallContext`。 |
| REPL 插件能力 | 插件提供给 Python REPL 使用的模块或对象，例如桌面操作接口。 | `ReplCapability`、`PreparedReplCapability`。 |
| 桌面操作 | 通过本机界面读取或操作屏幕、窗口、控件、鼠标和键盘。 | `computer_use` 插件及其 Python 接口。 |
| MCP 服务 | 通过 MCP 连接向应用提供工具等能力的服务。服务可以由本机程序或 HTTP 连接提供。 | `MCPConnectionConfig`、`MCPConnectionManager`。 |
| MCP 服务连接配置 | 连接某个 MCP 服务需要的启动命令、参数、地址、请求头等配置。 | `StdioMCPConnectionConfig`、`StreamableHttpMCPConnectionConfig`。 |
| MCP 服务连接校验指纹 | 根据 MCP 服务连接配置中的关键字段生成的校验值，用于确认当前连接是否仍对应获准的服务配置。 | `identity_fingerprint`、`server_identity_fingerprint()`。不写“服务身份”。 |

## 九、编号和容易混淆的字段

编号说明写清“哪个对象的编号”。同名字段在不同类型中的含义，应按所在类型解释。

| 标准术语 | 代码字段 | 含义 |
| --- | --- | --- |
| 主会话编号 | `ChatSettings.session_id`、`SubagentDefinition.session_id` | 子 Agent、权限和配置所属的主会话。 |
| 子 Agent 编号 | `subagent_id` | 某个子 Agent，同时用来区分该子 Agent 对话。 |
| Agent 对话编号 | `context_id`、`AgentState.session_id` | 主会话使用主会话编号；子 Agent 对话使用子 Agent 编号。 |
| 执行轮次编号 | `run_id` | 某个 Agent 的一次执行轮次。 |
| 目标执行轮次编号 | `target_run_id`、`expected_run_id` | 本次停止、引导等操作针对的执行轮次。 |
| 来源执行轮次编号 | `origin_run_id` | 创建任务的执行轮次；任务继续运行时保持原值。 |
| 工具调用编号 | `tool_call_id` | 某一次工具调用，用于匹配对应工具结果或审批。 |
| 来源工具调用编号 | `origin_tool_call_id` | 创建任务的那次工具调用。 |
| 等待请求编号 | 等待请求中的 `request_id` | 某一次用户审批或等待主 Agent 回复的请求。具体使用时优先写“审批请求编号”或“主 Agent 回复请求编号”。 |
| 输入编号 | `InboxEnvelope.id`、`input_sequence` | SessionInbox 中的一条输入。 |
| 消息编号 | `message_id` | Agent 对话中的某条消息，用于历史查询和界面去重。 |
| 历史记录编号 | 对话历史表的 `id` | 数据库中的一条对话历史记录；记忆处理使用其截止编号限制查询范围。 |
| checkpoint 编号 | checkpoint 记录的 `id` | 数据库中某一条 checkpoint 记录。 |
| 任务编号 | `task_id` | 某项任务记录；委派任务也使用任务记录保存结果，需按记录种类解释。 |
| 来源记录编号 | `SubagentInteraction.source_id` | 区分某次主 Agent 与子 Agent 交互的来源，用于避免重复保存同一请求。 |
| 应用启动批次编号 | `stream_epoch` | 当前界面事件序号所属的编号批次。 |
| 会话事件序号 | `event_sequence` | 某个主会话在该应用启动批次内的一条界面事件序号。 |

## 十、现有抽象表述的替换规则

替换时先确认对象和功能，再选用标准术语。下表用于改写注释，不要求改动已有代码标识符。

| 现有表述 | 应采用的表述 |
| --- | --- |
| 水位、输出水位、事件水位 | 指数字时写“某会话的最晚已发布事件序号”；涉及 `watermark()` 的完整返回值时写“应用启动批次编号和该会话的最晚已发布事件序号”。 |
| 父会话、父上下文 | 主会话。 |
| 子、子侧、子执行 | 根据所指对象写“子 Agent”“子 Agent 对话”或“子 Agent 的执行轮次”。 |
| 父、父侧、父身份 | 根据所指对象写“主 Agent”“主会话”或“主会话编号”。 |
| 子身份、稳定身份 | 根据所指对象写“子 Agent 编号”“子 Agent 定义”或“子 Agent 记录”。 |
| 身份资格、身份保护点 | 写出检查内容，例如“检查主会话配置是否存在”“检查子 Agent 是否已废弃”。 |
| 启动身份、epoch 身份 | 应用启动批次编号。 |
| 工具身份 | 工具标识。 |
| 服务身份 | 涉及配置校验时写“MCP 服务连接校验指纹”；涉及某项服务时写“MCP 服务编号”。 |
| 持久输入、持久通知 | 按内容写“待处理输入”“已保存的插件通知”或“任务结果通知”。 |
| 等待材料 | 按等待原因写“审批请求”或“主 Agent 回复请求”。 |
| 保存材料、配置材料 | 写出实际内容，例如“待保存的工具结果”“记忆操作配置”。 |
| 领取输入、消费输入 | 按操作分别写“查询待处理输入”“交付输入”或“处理输入”。 |
| 闭合协议、补齐协议 | 工具调用与结果配对；描述动作时写“为未完成的工具调用保存结束结果”。 |
| 开放入口、封闭入口 | 写出操作，例如“允许受理新的交互命令”“停止接受新的委派任务”。 |
| 激活意图、真人意图 | 写出请求，例如“用户消息”“启动会话的请求”。 |
| 上次进程、本次进程 | 描述应用运行范围时写“上次启动”“本次启动”；涉及操作系统进程时保留“进程”。 |
| 快照、会话快照 | 按用途写“checkpoint”“Agent 执行状态快照”“会话状态快照”或“任务状态快照”。 |
| 恢复执行 | 按实际行为写“加载 checkpoint”“恢复已保存状态”或“审批恢复”。 |
| 原子提交、持久提交、落盘 | 用途说明写“保存哪些内容”；对应实现旁再解释哪些数据在同一事务中保存。 |
| 生产者、消费者 | 在讲具体功能时写“发送事件的组件”“运行时请求队列处理循环”等实际对象；解释队列实现时可使用通用术语。 |

## 十一、注释改写示例

示例展示用途说明的写法。具体参数、返回值和异常按对应定义补充。

| 对应对象 | 改写后的用途说明 |
| --- | --- |
| `AgentRuntimeScheduler` | 管理后端运行期间的用户操作、主会话、子 Agent 和界面事件。 |
| `ParentSession` | 处理主会话收到的消息和用户操作，决定何时开始、继续或停止主 Agent 的执行。 |
| `ParentSession._wakeup` | 收到新输入、审批决定或关闭请求时，提醒会话处理循环重新检查需要处理的工作。 |
| `AgentRuntimeScheduler._reset_inbox()` | 后端启动时，删除 SessionInbox 中上次启动尚未交付的用户消息，保留任务结果通知和插件通知。 |
| `AgentRuntimeScheduler._finalize_abandoned_sessions()` | 后端启动时，为上次启动中断的主 Agent 执行轮次补充工具结果，并保存类型为 `COMPLETED` 的 checkpoint。 |
| `CheckpointType` | 标明 checkpoint 对应的执行轮次是否已经结束。`INTERMEDIATE` 表示仍在执行或等待，`COMPLETED` 表示执行轮次已结束。 |
| `InteractionEventHub.watermark()` | 查询当前主会话的应用启动批次编号和最晚已发布事件序号，供界面衔接会话状态快照之后的事件。 |
| `AgentIdentityService.read_availability()` | 检查主会话配置是否存在，以及指定子 Agent 是否属于该主会话并且尚未废弃。 |

改写前可用以下问题检查一句注释：

- 读者能否知道这段代码用于哪个功能？
- 是否写清处理的是主会话、子 Agent、执行轮次、模型请求还是后台任务？
- “保存”“加载”“恢复”“停止”是否有明确对象？
- 是否用标准术语区分 checkpoint、对话历史和状态快照？
- 每项补充说明是否直接帮助调用者理解用途、参数、结果或失败条件？
- 锁、事务、重试等实现说明是否放在对应代码旁？

## 代码核对入口

- 对话和执行管理：[AgentRuntimeScheduler](../app/agent/runtime/agent_runtime_scheduler.py)、[ParentSession](../app/agent/runtime/parent_session.py)、[SubagentSession](../app/agent/runtime/subagent_session.py)、[AgentExecution](../app/agent/runtime/agent_execution.py)。
- 类型和状态：[运行时类型](../app/schemas/runtime_types.py)、[子 Agent 类型](../app/schemas/subagent_types.py)、[任务类型](../app/schemas/task_types.py)。
- 消息和保存：[消息](../app/agent/state/messages.py)、[Agent 状态](../app/agent/state/model.py)、[SessionStateManager](../app/agent/state/manager.py)、[SessionInboxDao](../app/crud/session_inbox_dao.py)。
- 界面事件：[交互命令和界面事件](../app/interaction/interaction_contract.py)、[InteractionEventHub](../app/interaction/interaction_event_hub.py)。
- 工具和插件：[工具标识](../app/schemas/tool_identity.py)、[工具审批类型](../app/agent/tools/authorization/models.py)、[插件定义](../app/agent/plugins/base.py)。
- 模型、记忆和桌面功能：[模型接口类型](../app/agent/models/contracts.py)、[ModelInputCoordinator](../app/agent/context/coordinator.py)、[记忆插件](../app/agent/plugins/memory/memory.py)、[ReplService](../app/repl/repl_service.py)、[桌面操作接口](../app/agent/plugins/computer_use/repl/ayaya_computer_use/api.py)、[MCP 配置](../app/schemas/mcp.py)。
