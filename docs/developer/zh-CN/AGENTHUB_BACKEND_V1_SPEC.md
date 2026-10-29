# AgentHub 后端第一版规格草案

日期：2026 年 10 月 10 日。版本：0.1。用途：供业务、前端、API、worker 和云端交付负责人约定第一版行为，并据此实现与验收。

文档定位于后续讨论形成的技术细化建议。完整后端的范围和原始方向见 [后端总体规划](AGENTHUB_BACKEND_PLAN.md)，首个开发里程碑见 [最早期开发规划](AGENTHUB_BACKEND_INITIAL_DEVELOPMENT_PLAN.md)。具体政策、路由、状态与参数尚未成为团队确认的决定。

第一版提供保存聊天、查询工单与订单、判断退款资格、准备具体处置方案、人工审批及可恢复的模拟执行。模型提出回答和草案；应用代码决定访问权限、业务资格与实际执行。

## 依据与决定状态

依据为 [AgentHub 团队交接计划](../../AGENTHUB_TEAM_HANDOVER.md) 及其两张架构和审批流程图。以下区分三种状态：

| 状态 | 内容 |
| --- | --- |
| 已确认方向 | 范围受控、基本聊天、持久任务、人工审批、早期 CI/CD、适合团队经验的简单实现 |
| 沿用当前方案 | 客服与退款场景、FastAPI、Python worker、PostgreSQL 队列、SSE、独立模拟业务服务 |
| 建议规格 | 本文所有具体政策、字段、路由、状态名、认证方案、并发规则和数值参数，均为供团队审阅的第一版建议 |

本文给出可实现的默认选择，不代表这些新增细节已经获得团队共识，也不代表功能或验收已经完成。交接计划中的云部署、监控、备份与 CI/CD 继续保留；本文聚焦后端行为。

## 业务规则

### 范围与请求分类

第一版只有一个客服工作流，业务数据和操作均为模拟。客户回复记录必须携带 is_simulated=true，界面与最终回复应明确说明“模拟退款”“模拟客户回复”。没有真实支付、邮件或外部客户通知。

| 用户请求 | 处理规则 | 是否创建 execution |
| --- | --- | --- |
| 普通知识问答 | 保存并后台生成回答，不调用业务工具 | 否 |
| 目标或意图不清楚 | 保存澄清问题，本次处理结束；用户回答作为新消息 | 路由时发现则否 |
| 查询工单、订单或退款资格 | 只读任务，保存结果并展示 | 是 |
| 请求准备或处理客服处置 | 查询与检查，准备具体方案；有业务变更时等待审批 | 是 |

“检查是否可以退款”只授权检查。准备退款方案也不代表已经执行退款。工单文本、工具结果和模型生成的“已获批准”不能提供执行授权。

任务中的模式为 READ_ONLY 或 PREPARE_RESOLUTION，由首次路由结合原始用户请求确定并保存。后续模型调用不能根据工单文本自行把 READ_ONLY 升级为写入流程。实际写入仍必须通过审批检查。

任务执行中发现缺信息时，保存澄清问题，以 COMPLETED、outcome_code=NEEDS_INFORMATION 结束并释放会话。第一版不新增 WAITING_FOR_INPUT，不通过聊天修改运行中的任务。

### 数据访问与会话并发

建议第一版采用用户级所有权，不引入组织、共享会话或独立审批员角色：

- conversation.owner_user_id 是会话、消息、任务、事件和方案的访问依据。
- execution.owner_user_id 必须等于所属会话的所有者。
- 只有方案所属任务的用户可以批准或拒绝该方案。
- 工单和订单分别有 assigned_user_id；读取及执行都要求与执行所有者一致。关联工单与订单的归属也必须一致。
- API 从 session 获取身份，worker 从已保存的 job/execution 获取身份；模型不得提供或覆盖 actor_user_id。
- 未找到和无权访问的资源均对用户返回 NOT_FOUND，不泄露其他用户是否拥有该 ID。

建议每个会话同时最多有一个活动输入，覆盖路由、普通聊天、业务执行和 WAITING_FOR_APPROVAL。用 conversation.active_message_id 持久表示占用；新消息返回 409 CONVERSATION_BUSY。其他会话仍可提交工作。

等待审批期间仍可查询历史和提交审批，但该会话不能继续发送聊天。正常终态、拒绝或方案失效后清除占用。RECOVERY_REQUIRED 保留占用，避免结果未确认时开始新任务。

同一提交的重发应先查去重记录，再检查 busy；已受理的重复请求返回原受理结果。

### 演示退款政策

建议政策 ID 为 DEMO-CAD-30D，version=1。它仅是项目演示政策，不是实际商家的政策或退款法律。

| 条件 | 建议规则 |
| --- | --- |
| 付款 | payment_status=PAID，paid_total_minor 为正整数 |
| 交付 | fulfillment_status=DELIVERED，delivered_at 不在未来 |
| 币种 | 仅 CAD |
| 时间窗口 | 服务端 now <= delivered_at + 30 × 24 小时，边界包含等号 |
| 既往退款 | 没有该订单的退款记录 |
| 金额 | 只能全额退 paid_total_minor；不支持部分退款、手续费和税额重算 |
| 次数 | 每个订单最多一次退款，由数据库唯一约束保证 |

资格结果是 ELIGIBLE、INELIGIBLE 或 NEEDS_INFORMATION：

- ELIGIBLE 返回准确全额金额、政策版本、订单版本和 checked_at。
- INELIGIBLE 返回确定原因，如 NOT_PAID、NOT_DELIVERED、WINDOW_EXPIRED、ALREADY_REFUNDED、UNSUPPORTED_CURRENCY。
- 缺少或存在不合法的必需数据时返回 NEEDS_INFORMATION，例如缺交付时间、金额或金额格式不合法。已有确定拒绝原因时无需因无关缺字段改变为未知。
- 不存在或无权访问返回工具错误 NOT_FOUND，与不符合资格区分。

订单金额和时间从业务数据读取，不能采用模型提交的事实。运行时使用数据库/业务服务 UTC 时钟；测试可注入固定时钟，但公共接口不接受用户提供的 now。

### 获批处置的范围

建议第一版只有一种处置：向 OPEN 工单保存一条明确的模拟客户回复，把状态改为 RESOLVED，并可选地执行一次全额模拟退款。

查询、检查资格、在聊天里展示草稿和保存内部进度无需额外审批。保存模拟客户回复、关闭工单、退款均是业务变更，即使不退款也必须批准整份方案。

如果请求仅为“准备退款，如符合条件”，不符合资格时返回只读结论。只有用户同时请求处理回复/工单时，才可提出无退款的回复与关闭方案。缺信息时结束并澄清，不用推测生成可执行方案。

已 RESOLVED 工单仍可读取，但不能再次应用处置。仅知道订单、却没有明确工单时，可以检查资格；需要处置时必须明确工单。一个订单可能关联多个工单，系统不能任意选择其中一个。

第一版处置必须有已关联、可访问的订单；ticket.order_id 为空或所需订单数据缺失时，返回 NEEDS_INFORMATION，不创建方案。无退款处置不要求资格为 ELIGIBLE，可以用于已确定 INELIGIBLE 的订单，但仍检查授权、关联关系、有效期和预期版本。只有 refund 非空时才要求当前 ELIGIBLE 及准确全额金额。

一份方案必须冻结 ticket_id、order_id、可选退款、回复原文、工单状态变更、预期业务版本、政策版本、证据和有效期。保存后不提供编辑接口。第一版每个 execution 最多一份方案，version=1；失效后由新消息创建新任务。

建议方案 expires_at 为创建后 24 小时；退款方案还不得晚于退款窗口截止时间。now > expires_at 时失效。批准时及执行时都检查有效期；执行时重新验证版本和资格。变化导致整份方案 INVALIDATED，不自动换金额、目标或回复。

### 处置原子性与幂等

建议模拟服务使用与应用相同的 PostgreSQL 实例、独立 business schema。模拟服务是独立服务进程；API 和 worker 通过受保护的内部接口调用它。

一次新处置在同一数据库事务中写入模拟回复、可选退款、工单状态和 action_results。任何校验失败则整份处置不写业务数据。第一版不需要实现多个独立动作的部分成功恢复。

action_key 在方案创建时生成并持久化，同一方案的所有尝试使用同一个 key。业务服务先查询该 key 的既有结果：proposal_id 和 payload_hash 相同则返回已有结果；不相同则拒绝。只有没有既有结果的新动作，才执行资格、版本、有效期和授权检查。

此顺序很重要：第一次成功会改变订单和工单版本，重试不能把自己的已成功动作判成方案过期。

不同 action_key 竞争同一订单时，版本检查、订单行锁和 refunds.order_id UNIQUE 防止重复退款；失效的一方也不能继续写回复或关闭工单。

每次处置增加 ticket.version；只有退款或其他订单字段实际变化才增加 order.version。不同工单关联同一订单、且均无退款的处置可以独立完成；同一工单的重复处置和同一订单的重复退款被拒绝。

### 样例数据

seed_time 由初始化程序记录，日期采用相对偏移；独立测试使用独立 fixture 或重置数据库。

| 工单与订单 | 指派用户 | 金额 | 交付数据 | 用途 |
| --- | --- | --- | --- | --- |
| 391 / 1001 | Alice | 12999 分 CAD | seed_time 前 5 天，已付已交付 | 正常退款 |
| 392 / 1002 | Alice | 4500 分 CAD | seed_time 前 45 天，已付已交付 | 超期 |
| 393 / 1003 | Alice | 7999 分 CAD | 已交付但 delivered_at 缺失 | 缺信息 |
| 395 / 1005 | Bob | 10000 分 CAD | seed_time 前 3 天，已付已交付 | 越权访问 |

“已退款”案例可以先完成 391 的获批处置，再从另一个会话检查订单 1001，避免为 seed 引入一套没有审批历史的特殊动作记录。

## 数据关系

### 记录含义

每条用户消息创建一个 PROCESS_MESSAGE job。普通聊天和路由阶段没有 execution；第一次接受有效业务工具调用时，才创建 execution 并关联该 job。

等待审批时，当前 job 进入 SUCCEEDED，因为这一段后台工作已经完成；execution 保持 WAITING_FOR_APPROVAL。批准后创建一个新的 RESUME_EXECUTION job。job 代表一次待调度工作，execution 代表贯穿多个 job 的业务任务。

~~~mermaid
erDiagram
    USER ||--o{ CONVERSATION : owns
    CONVERSATION ||--o{ MESSAGE : contains
    MESSAGE ||--o| EXECUTION : starts
    MESSAGE ||--o| JOB : initial_work
    EXECUTION ||--o{ JOB : processed_by
    EXECUTION ||--o{ TOOL_CALL : records
    EXECUTION ||--o| PROPOSAL : prepares
    PROPOSAL ||--o| APPROVAL : decided_by
    CONVERSATION ||--o{ EVENT : records
    ORDER ||--o{ TICKET : associated_with
    ORDER ||--o| REFUND : refunded_once
    TICKET ||--o{ BUSINESS_REPLY : records
    PROPOSAL ||--o| ACTION_RESULT : applied_once
~~~

这是逻辑关系；应用表中补充 FK 与约束。MESSAGE 到 JOB 的 initial_work 仅指 PROCESS_MESSAGE；恢复 job 通过 execution 关联原始消息。

### 应用 schema 建议

应用 UUID 由服务端生成，时间使用 timestamptz。JSON 字段用于有限结构的 checkpoint、证据与结果，不代替关键外键或唯一约束。

| 表 | 最少字段 | 关键约束 |
| --- | --- | --- |
| app.users | id, email, password_hash, created_at | 规范化 email 唯一；密码不能明文保存 |
| app.sessions | token_hash, user_id 可空, csrf_token, expires_at | session 原始 token 不落库；user_id 为空只用于登录前 CSRF |
| app.conversations | id, owner_user_id, title, active_message_id 可空, last_message_seq, last_event_seq, created_at | owner FK；active_message 必须属于该会话 |
| app.messages | id, conversation_id, seq, role, content, in_reply_to 可空, created_at | UNIQUE(conversation_id, seq)；role=USER/ASSISTANT；ASSISTANT 的 in_reply_to 指向同会话 USER，且唯一 |
| app.jobs | id, kind, queue_key, conversation_id, source_message_id, execution_id 可空, submission_key 可空, request_hash 可空, submission_receipt 可空, status, worker_id 可空, claim_version, lease_expires_at 可空, attempt_count, available_at, checkpoint, last_error 可空, created_at | queue_key 唯一；PROCESS_MESSAGE 对 source_message_id 唯一，并对 (conversation_id, submission_key) 唯一；领取信息与状态合法匹配 |
| app.executions | id, conversation_id, owner_user_id, originating_message_id, current_job_id, mode, status, stage, checkpoint, model_call_count, tool_call_count, outcome_code 可空, result 可空, error 可空, recovery_reason 可空, recovery_generation, created_at, updated_at | originating_message_id 唯一；owner 与 conversation 一致；current_job 的 execution/来源一致；recovery_generation 从 0 开始 |
| app.tool_calls | id, execution_id, step_no, tool_name, arguments, status, result 可空, error 可空, started_at, completed_at 可空 | UNIQUE(execution_id, step_no)；保存被接受的参数与结果 |
| app.proposals | id, execution_id, version, action, action_hash, action_key, evidence, expires_at, invalidated_at 可空, invalidated_reason 可空, created_at | execution_id 唯一；action_key 唯一；action/hash/key/证据/有效期不可修改 |
| app.approvals | id, proposal_id, proposal_version, action_hash, user_id, decision, decided_at | proposal_id 唯一；decision=APPROVE/REJECT；记录后不修改 |
| app.events | conversation_id, seq, type, message_id 可空, execution_id 可空, payload, created_at | PRIMARY KEY(conversation_id, seq)；与相关状态变更同事务 |

建议 jobs.kind 只有 PROCESS_MESSAGE、RESUME_EXECUTION。queue_key 分别为 process:<message_id>、approval:<approval_id> 或 recovery:<execution_id>:<generation>。恢复可能产生多个 job，不能对所有 RESUME_EXECUTION 使用 execution_id 单列唯一约束。

source_message_id、conversation_id、owner 等冗余连接信息必须在创建时验证一致；不能让客户端自行指定 owner。

消息 seq 和事件 seq 是不同计数器。分配事件 seq 时锁 conversation 行、递增 last_event_seq 并插入事件，在同一事务提交。不能直接用全局自增 ID 充当“已经提交的事件前缀”，否则后提交的较小 ID 可能被游标跳过。

第一版每条 USER 消息最多保存一条最终 ASSISTANT 回复，普通回答、澄清、拒绝、失效或任务结论均通过 in_reply_to 关联。中间进度使用 event，待审批展示使用 proposal 卡片；不为每次重试生成新的聊天回复。

tool_calls.status 建议为 PENDING、RUNNING、SUCCEEDED、FAILED。接受模型调用时，在同一事务分配固定 step_no、保存完整参数和 pending-step checkpoint。重试同一步复用原记录，不生成新的逻辑步骤。已有成功结果则直接读取；已接受但没有结果则重试相同 step 和参数，不能再次请求模型重新生成调用。只有接受新的模型响应才分配新 step；写调用始终复用同 action_key。

### 业务 schema 建议

| 表 | 最少字段 | 关键约束 |
| --- | --- | --- |
| business.orders | id, assigned_user_id, currency, paid_total_minor 可空, payment_status, fulfillment_status, delivered_at 可空, version | ID 为独立正整数；version 从 1 开始；重要字段变化递增 version |
| business.tickets | id, order_id 可空, assigned_user_id, customer_label, subject, body, status, version | order FK；status=OPEN/RESOLVED；重要字段变化递增 version |
| business.policy | id, version, currency, refund_window_days | 第一版一条 DEMO-CAD-30D；政策变更递增 version |
| business.refunds | id, order_id, amount_minor, currency, action_key, created_at, is_simulated | order_id 唯一；action_key 唯一；amount_minor>0；is_simulated=true |
| business.replies | id, ticket_id, body, action_key, created_at, is_simulated | action_key 唯一；ticket FK；is_simulated=true |
| business.action_results | action_key, proposal_id, payload_hash, result, created_at | action_key 主键；proposal_id 唯一；保存可重放的完整结果 |

退款结果是资格判断中“是否已退款”的权威记录，无需另外维护容易不一致的 refunded 布尔值。应用的 result 是业务 action_result 的展示副本。

按角色限制数据库权限：API 不写业务表；worker 不直接修改订单/工单；模拟服务可以写 business schema，并读取及锁定执行授权所需的 app 记录。行锁所需权限应单独配置；无需授予模拟服务任意修改任务状态的权限。迁移账号与运行账号分开。

### Checkpoint 建议

路由前的模型输入快照、调用计数和已接受响应保存在 job.checkpoint。创建 execution 后，任务恢复依据以 execution.checkpoint 为主。

至少保存 mode、原始目标、当前 stage、已接受的模型响应、待执行的固定工具参数、已完成工具结果引用、资格证据、proposal_id、action_key，以及已确认的 action_result。保存模型输入/输出中恢复所需内容，不保存模型内部推理过程。

~~~json
{
  "schema_version": 1,
  "stage": "APPLY_RESOLUTION",
  "mode": "PREPARE_RESOLUTION",
  "ticket_id": 391,
  "order_id": 1001,
  "completed_tool_steps": [1, 2],
  "proposal_id": "50000000-0000-4000-8000-000000000001",
  "action_key": "70000000-0000-4000-8000-000000000001",
  "action_request_started": true,
  "action_result": null
}
~~~

action_request_started 在发送内部执行请求前持久化。恢复时该字段为 true 且没有结果，必须查同 action_key 的权威记录，再决定重放；不能重新生成方案或换 key。已有 action_result 时只能收尾。

## 接口契约

### 通用约定与认证

前缀为 /api/v1；JSON 字段使用 snake_case；应用资源 ID 为 UUID，ticket_id/order_id 为不同命名空间的正整数。时间为 UTC ISO 8601，例如 2026-10-10T12:00:00Z；金额为整数 minor units，12999 表示 CAD 129.99。

建议同源部署，使用 opaque session cookie；生产环境 Secure=true、HttpOnly=true、SameSite=Lax、Path=/，不设置 Domain。本地 HTTP 开发可显式关闭 Secure，但不能把该配置用于云端。密码哈希库与具体参数在实现时选择。

所有变更请求携带 X-CSRF-Token 并检查 Origin。GET /auth/session 返回与当前 session 绑定的 CSRF token，登录前使用匿名 session；登录成功旋转 session 和 CSRF，退出撤销 session。session 建议有效 8 小时。token 不放入 URL、日志或模型上下文。cookie 属性与 CSRF 的用途参考 [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) 和 [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)；8 小时与同源选择是本规格建议。

除健康检查、登录和匿名 session 获取外，均要求登录。所有资源接口、SSE 和业务工具都检查访问权限。第一版账户由初始化程序创建，不新增公开注册、角色管理或组织管理接口。

### 路由清单

| 方法与路径 | 输入 | 成功响应 |
| --- | --- | --- |
| GET /health/live | 无 | 200，进程存活 |
| GET /health/ready | 无 | 200，数据库与 schema 可用；不可用返回 503 |
| GET /api/v1/auth/session | cookie 可选 | 200，user 或 null、csrf_token、expires_at；Cache-Control: no-store |
| POST /api/v1/auth/login | email, password；CSRF | 200，user、新的 csrf_token 与 cookie；凭据错误 401 |
| POST /api/v1/auth/logout | CSRF | 204，撤销 session |
| POST /api/v1/conversations | title 可选 | 201，Conversation |
| GET /api/v1/conversations | limit 默认 20、cursor 可选 | 200，items、next_cursor |
| GET /api/v1/conversations/{id} | UUID | 200，Conversation，含 active_message_id |
| POST /api/v1/conversations/{id}/messages | content；Idempotency-Key；CSRF | 202，固定受理 receipt |
| GET /api/v1/conversations/{id}/messages | after_seq 默认 0、limit 默认 50 | 200，items、next_after_seq、has_more、snapshot_event_seq |
| GET /api/v1/conversations/{id}/executions | limit 默认 20、cursor 可选 | 200，执行摘要列表、next_cursor |
| GET /api/v1/executions/{id} | UUID | 200，ExecutionDetail |
| GET /api/v1/proposals/{id} | UUID | 200，ProposalDetail |
| POST /api/v1/proposals/{id}/decision | decision, version, action_hash；CSRF | 首次 201，相同已记录决定 200 |
| GET /api/v1/conversations/{id}/events | after 可选；Last-Event-ID 可选 | 200，text/event-stream |

列表 limit 允许 1–100。会话与执行列表按 created_at、id 升序使用不透明游标；消息按 seq 升序。消息 content 必须是去除首尾空白后 1–4000 个 Unicode 字符；title 最多 100 字符；客户回复最多 2000 字符。未列出的输入字段拒绝，避免用户提交 owner、status、approval 等服务器字段。

Conversation 返回 id、title、created_at、active_message_id、last_message_seq、last_event_seq。Message 返回 id、conversation_id、seq、role、content、in_reply_to、created_at、execution_id 和 processing_state。

USER 消息的 processing_state 是派生字段：有 execution 时用 execution.status；没有 execution 时，初始 job 的 QUEUED/RUNNING 均显示 PENDING，SUCCEEDED/FAILED 映射为 COMPLETED/FAILED。第一版不实时区分普通聊天的排队与运行；job 领取不改变它的公开 PENDING 状态。ASSISTANT 消息没有自己的 job，processing_state 固定为 COMPLETED，execution_id 通过 in_reply_to 的 USER 消息关联。该字段不额外落库。

列表游标只在所属资源和排序方式内有效。ExecutionDetail 返回公开 status、stage、mode、outcome_code、current_job_status、proposal、result、error 和 recovery_reason，不直接暴露内部 checkpoint、session、worker 凭据或完整模型上下文。

### 提交消息与请求去重

Idempotency-Key 建议使用客户端生成的 UUID。去重范围为 user_id + conversation_id + key；会话 owner 不变时，PROCESS_MESSAGE 的 UNIQUE(conversation_id, submission_key) 即可保证该范围。消息提交的原始 key、规范化内容摘要和固定 receipt 分别保存在初始 job 的 submission_key、request_hash、submission_receipt，不随着 checkpoint 更新或重试覆盖。queue_key 保证调度触发唯一，与客户端提交 key 的用途不同。

规范化只去除首尾空白，不折叠内部空白。先确认会话权限，再在 conversation 行锁内检查已有 key。同 key、同内容返回原 202；同 key、不同内容返回 409 IDEMPOTENCY_CONFLICT。没有已有 key 时再检查 busy。

~~~json
{
  "content": "查看工单 391，准备回复，并在符合条件时准备全额退款。"
}
~~~

~~~json
{
  "message_id": "20000000-0000-4000-8000-000000000001",
  "job_id": "30000000-0000-4000-8000-000000000001",
  "execution_id": null,
  "accepted_at": "2026-10-10T12:00:00Z"
}
~~~

execution_id=null 表示受理时尚未创建业务 execution，不代表将来没有任务。重复受理 receipt 保持原值；最新状态从查询和 SSE 获取。只保存消息、未入队或未持有会话占用，都不算成功受理。

同一规范化内容使用新 key 视为新请求；业务唯一约束仍须防止不同请求重复退款。去重记录在第一版随会话数据保留。

### 方案与审批

以下是 ProposalDetail 的建议结构；ID 与 hash 为结构示例，hash 在实现中必须由服务器按确定的规范化规则计算。

~~~json
{
  "id": "50000000-0000-4000-8000-000000000001",
  "execution_id": "40000000-0000-4000-8000-000000000001",
  "version": 1,
  "action_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "status": "PENDING",
  "expires_at": "2026-10-11T12:00:00Z",
  "action": {
    "ticket_id": 391,
    "order_id": 1001,
    "refund": {"amount_minor": 12999, "currency": "CAD"},
    "customer_reply": "订单符合演示退款条件，将执行模拟全额退款。",
    "ticket_update": {"status": "RESOLVED"},
    "expected_versions": {"ticket": 1, "order": 1, "policy": 1},
    "policy_id": "DEMO-CAD-30D",
    "is_simulated": true
  },
  "evidence": {
    "eligibility_status": "ELIGIBLE",
    "checked_at": "2026-10-10T12:00:03Z",
    "reason_codes": [],
    "refund_window_end": "2026-11-04T12:00:00Z"
  },
  "decision": null
}
~~~

refund=null 表示无退款处置。order_id、工单与订单关联、金额、币种、预期版本必须由 worker 根据工具结果校验并填入，不能直接采用模型的业务事实。

建议 action_hash 为 action 的 SHA-256：对象键递归排序，UTF-8，禁止 NaN，整数金额，紧凑 JSON 分隔符，所有默认字段显式补齐；action 的文本保存后不再规范化。proposal_id/version 在审批中独立绑定，服务端不信任客户端重算 hash。

ProposalDetail.status 为 PENDING、APPROVED、REJECTED 或 INVALIDATED，来自唯一 approval 和失效字段；未维护第二套可独立变化的审批事实。若获批后方案失效，保留原 APPROVE 决定，status=INVALIDATED。

~~~json
{
  "decision": "APPROVE",
  "version": 1,
  "action_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
~~~

~~~json
{
  "approval_id": "60000000-0000-4000-8000-000000000001",
  "proposal_id": "50000000-0000-4000-8000-000000000001",
  "decision": "APPROVE",
  "decided_at": "2026-10-10T12:01:00Z",
  "execution_id": "40000000-0000-4000-8000-000000000001",
  "resume_job_id": "30000000-0000-4000-8000-000000000002"
}
~~~

审批事务先检查权限与方案 version/hash。已有相同决定则返回已有 receipt，不再入队，即使之后方案失效；已有相反决定返回 409 DECISION_CONFLICT。没有决定时，必须处于 WAITING_FOR_APPROVAL 且未过期；批准、execution 入队和恢复 job 原子提交。

批准时必须同时设置 execution.current_job_id=new_job.id。重复 APPROVE 的 resume_job_id 从 queue_key=approval:<approval_id> 的原始批准 job 获取，不能取可能已改变的 current_job_id；REJECT 的 resume_job_id 始终为 null。

拒绝响应 decision=REJECT、resume_job_id=null，execution=REJECTED。过期的首次决定返回 409 PROPOSAL_STALE，并在同一事务保存失效、释放会话、写事件。不接受聊天中的“批准了”作为审批记录。

### 错误契约

错误返回统一 JSON，request_id 用于日志关联；不要返回密钥、完整 SQL、内部异常栈或其他用户信息。

~~~json
{
  "error": {
    "code": "CONVERSATION_BUSY",
    "message": "当前会话有未结束的工作，请先完成审批或等待结果。",
    "request_id": "80000000-0000-4000-8000-000000000001",
    "details": {
      "active_message_id": "20000000-0000-4000-8000-000000000001"
    }
  }
}
~~~

| HTTP | code | 含义 |
| --- | --- | --- |
| 401 | UNAUTHENTICATED / INVALID_CREDENTIALS | 未登录、session 过期或登录失败 |
| 403 | CSRF_FAILED | CSRF 或来源检查失败 |
| 404 | NOT_FOUND | 资源不存在或当前用户不可访问 |
| 409 | CONVERSATION_BUSY | 会话已有活动工作 |
| 409 | IDEMPOTENCY_CONFLICT | 同一提交 key 使用不同内容 |
| 409 | DECISION_CONFLICT | 已存在相反审批决定 |
| 409 | PROPOSAL_MISMATCH / PROPOSAL_STALE / INVALID_STATE | 方案版本或 hash 不符、方案失效或状态不允许 |
| 422 | VALIDATION_ERROR / INVALID_CURSOR | 参数类型、长度、未知字段或游标非法 |
| 503 | SERVICE_UNAVAILABLE | 数据库等基础服务不可用；不能返回已受理 |

“不符合退款资格”是正常业务结果。提交消息后异步发现的 NOT_FOUND、缺信息等通过结果/事件展示；不能把已返回 202 的请求改成同步 HTTP 错误。

### SSE 与历史快照

事件 envelope：

~~~json
{
  "seq": 8,
  "type": "proposal.created",
  "conversation_id": "10000000-0000-4000-8000-000000000001",
  "message_id": "20000000-0000-4000-8000-000000000001",
  "execution_id": "40000000-0000-4000-8000-000000000001",
  "created_at": "2026-10-10T12:00:04Z",
  "payload": {
    "proposal_id": "50000000-0000-4000-8000-000000000001",
    "version": 1,
    "status": "WAITING_FOR_APPROVAL"
  }
}
~~~

~~~text
id: 8
event: proposal.created
data: {"seq":8,"type":"proposal.created","conversation_id":"10000000-0000-4000-8000-000000000001","message_id":"20000000-0000-4000-8000-000000000001","execution_id":"40000000-0000-4000-8000-000000000001","created_at":"2026-10-10T12:00:04Z","payload":{"proposal_id":"50000000-0000-4000-8000-000000000001","version":1,"status":"WAITING_FOR_APPROVAL"}}

~~~

事件类型建议为 message.accepted、message.created、message.failed、execution.created、execution.status_changed、execution.progress、proposal.created、approval.decided。工具进度只展示适当摘要，不把内部工具参数或未授权数据直接放进事件。

SSE id 使用会话 seq；payload 的 event type 和 seq 必须与 envelope 一致。Last-Event-ID 有效时优先使用它，否则使用 after，默认 0；发送 seq 大于游标的已提交事件，按 seq 升序。无效、负数或大于当前最大值的游标在开启 stream 前返回 422。

客户端按 conversation_id + seq 去重，只在收到完整事件后推进游标；重连不重新提交消息。首次页面加载的历史及 snapshot_event_seq 必须在同一一致性快照事务中读取，随后从该快照游标开始 SSE。不能先读旧历史、再从另一次查询拿新游标，否则会跳过中间更新。翻页时继续沿用初次已建立的 SSE，不把后续分页游标当成新的实时起点。

前端初始化顺序固定为：先取消息快照与游标，立即从该游标建立 SSE，再查询当前 execution/proposal。事件作为刷新资源的通知，不直接用旧事件 payload 覆盖较新的 GET 结果。同一资源的刷新串行处理；刷新期间到达的新事件触发一次后续刷新。不能先读旧任务详情、再使用较新的消息游标作为 SSE 起点。

API 建议每 1 秒查询一次新事件、每 15 秒发送注释心跳；心跳不落库、不占 seq。代理须允许流式传输并避免缓冲。第一版保留所有会话事件；session 过期时关闭 stream，让前端重新认证。SSE 的 Last-Event-ID 与事件格式参考 [HTML SSE 标准](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header)；持久补发由本应用实现。

### 工具与模型契约

三个业务能力如下。它们只暴露在内部服务网络，使用服务身份认证；公共 API 没有直接退款执行接口。

| 工具 | 参数 | 结果与限制 |
| --- | --- | --- |
| get_case_context | ticket_id 或 order_id，恰好一个正整数 | ticket 或 null、order 或 null、policy、业务版本；按订单查询不擅自选择关联工单 |
| check_refund_eligibility | order_id 正整数 | status、reason_codes、refund_amount_minor 或 null、currency、order_version、policy_version、checked_at、refund_window_end 或 null |
| apply_resolution | proposal_id、version、action_hash | worker-only；读取已批准的保存方案，返回稳定的模拟 action_result |

内部 HTTP 建议映射为 POST /internal/v1/tools/get-case-context、POST /internal/v1/tools/check-refund-eligibility 和 POST /internal/v1/tools/apply-resolution。两个读工具返回 200；新动作与既有结果重放均返回 200，result.replayed 区分是否重放。工具错误采用相同 error envelope，包含 retryable 布尔值。

worker 在可信请求上下文另附 job_id、worker_id、claim_version、execution_id、actor_user_id；action_key 只用于执行或查询处置结果，两个读工具不要求它。这些不是模型参数；模拟服务从 app 的权威记录验证身份、归属和批准，不能只相信这些请求字段。

资格结果示例：

~~~json
{
  "status": "ELIGIBLE",
  "order_id": 1001,
  "reason_codes": [],
  "refund_amount_minor": 12999,
  "currency": "CAD",
  "order_version": 1,
  "policy_version": 1,
  "checked_at": "2026-10-10T12:00:03Z",
  "refund_window_end": "2026-11-04T12:00:00Z"
}
~~~

执行结果建议包含 action_key、proposal_id、ticket_id、order_id、refund_id 或 null、refund_amount_minor 或 null、reply_id、ticket_status、applied_at、is_simulated=true。稳定事实在 action_results 中保存；replayed 是每次调用的传输字段，不参与持久结果或 hash。

模型每次只返回一种经过验证的结构化结果：

| kind | 必需字段 | 处理 |
| --- | --- | --- |
| ANSWER | text | 保存普通回答，或结束已有只读 execution |
| CLARIFY | text | 保存问题并结束本次处理 |
| READ_TOOL_CALL | tool_name、arguments | 只允许两个读工具，保存接受的调用后执行 |
| PROPOSE_RESOLUTION | ticket_id、order_id、refund_requested、customer_reply、ticket_update | worker 从权威结果补全金额、版本、政策、证据，校验后保存方案 |

首次业务输出还包含 mode；后续调用沿用已保存 mode。PROPOSE_RESOLUTION 是模型输出类型，不是第四个业务工具。apply_resolution 不交给模型自行执行；批准后 worker 直接使用存储的方案，不能再次请模型改写它。

未知工具、额外字段、类型错误或目标不一致不能执行。工具结果文本属于数据，不能提供新的执行权限。第一版不实现任意代码工具、附件、RAG 或运行任务编辑。

## 状态转换与恢复

### Job 状态

| 当前 | 条件 | 下一状态 | 伴随操作 |
| --- | --- | --- | --- |
| 无 | 消息受理、批准或内部恢复安排 | QUEUED | 保存唯一 queue_key、available_at |
| QUEUED | 到达 available_at 并原子领取 | RUNNING | attempt_count+1、claim_version+1、设置 owner 与 lease |
| RUNNING | 当前工作段已持久完成 | SUCCEEDED | 清除 lease；业务任务可能仍等待审批 |
| RUNNING | 可重试错误或租约过期，仍有预算 | QUEUED | 撤销 owner、递增 claim_version、安排延迟 |
| RUNNING | 不可恢复或重试预算耗尽 | FAILED | 撤销 owner/lease，按 execution 规则决定终止或对账 |

job 状态不包含 WAITING_FOR_APPROVAL。领取、续期与回收由 PostgreSQL 状态驱动，不依赖某个 API 或 worker 的内存。

### Execution 状态与阶段

建议公开状态为 QUEUED、RUNNING、WAITING_FOR_APPROVAL、COMPLETED、REJECTED、INVALIDATED、FAILED、RECOVERY_REQUIRED。RECOVERY_REQUIRED 是结果未确认或收尾失败时的暂停状态，不是终态。

阶段独立于状态，建议为 READ_CONTEXT、CHECK_ELIGIBILITY、PREPARE_PROPOSAL、APPLY_RESOLUTION、FINALIZE。工具循环可以在只读阶段继续读取，但不得退回并改写获批方案。

~~~mermaid
stateDiagram-v2
    [*] --> RUNNING: 接受首个业务工具调用
    RUNNING --> COMPLETED: 只读完成或需要澄清
    RUNNING --> WAITING_FOR_APPROVAL: 保存固定方案
    WAITING_FOR_APPROVAL --> REJECTED: 拒绝
    WAITING_FOR_APPROVAL --> INVALIDATED: 过期
    WAITING_FOR_APPROVAL --> QUEUED: 批准并创建恢复job
    QUEUED --> RUNNING: worker领取
    RUNNING --> QUEUED: 可恢复错误或租约过期
    RUNNING --> INVALIDATED: 新动作验证发现方案失效
    RUNNING --> COMPLETED: 已确认动作并完成收尾
    RUNNING --> FAILED: 确认无业务动作且不能继续
    RUNNING --> RECOVERY_REQUIRED: 动作结果未知或收尾失败
    RECOVERY_REQUIRED --> QUEUED: 内部对账后安排恢复
    RECOVERY_REQUIRED --> INVALIDATED: 确认未执行且方案失效
    COMPLETED --> [*]
    REJECTED --> [*]
    INVALIDATED --> [*]
    FAILED --> [*]
~~~

创建 execution 时初始 job 已 RUNNING，因此初始公开状态直接为 RUNNING；QUEUED 用于批准后、重试和恢复后的等待。普通聊天在产生 execution 前，只展示 job 派生状态。

| 事件 | execution 的改变 | 会话与记录要求 |
| --- | --- | --- |
| 接受首个只读调用 | 无 → RUNNING | 一次创建并关联原消息与当前 job |
| 只读结束、缺信息或目标不可访问 | RUNNING → COMPLETED | 分别保存 READ_ONLY_RESULT、NEEDS_INFORMATION 或 NOT_FOUND；保存回答，释放占用 |
| 生成具体方案 | RUNNING → WAITING_FOR_APPROVAL | stage=APPLY_RESOLUTION；方案、checkpoint、事件及 job SUCCEEDED 同事务；保留占用 |
| 批准 | WAITING_FOR_APPROVAL → QUEUED | 唯一 APPROVE、恢复 job 和事件同事务 |
| 拒绝 | WAITING_FOR_APPROVAL → REJECTED | 保存 REJECT、回答与事件，释放占用；无业务写入 |
| 未执行方案过期或权威业务校验失败 | WAITING_FOR_APPROVAL/RUNNING → INVALIDATED | 原因 PROPOSAL_EXPIRED、BUSINESS_STATE_CHANGED 或 ELIGIBILITY_CHANGED；不产生部分处置；释放占用 |
| 普通可恢复错误或租约过期 | RUNNING → QUEUED | 保留 checkpoint，原 job 回队列，等待新 claim |
| 已获得成功动作结果 | 保持 RUNNING，stage=FINALIZE | 保存不可重复生成的 result；之后仅收尾 |
| 收尾完成 | RUNNING → COMPLETED | 保存 APPLIED_SIMULATED_RESOLUTION、最终消息、事件，释放占用 |
| 无业务写入且失败不可恢复 | RUNNING/QUEUED → FAILED | 保存清晰错误与消息，释放占用 |
| 动作可能发生但无法确认，或动作已确认而收尾失败 | RUNNING/QUEUED → RECOVERY_REQUIRED | 当前 job FAILED，保留占用/key/方案；展示真实的确认程度 |
| 内部对账确认未执行且方案已失效 | RECOVERY_REQUIRED → INVALIDATED | 排除旧调用提交后保存失效与消息、释放占用；结果未知不能走此出口 |

COMPLETED 表示本次处理完成，不一定退款成功；必须结合 outcome_code 和 result 展示。普通 FAILED 只用于能够确认无未决业务动作的失败，不能把结果未知描述为“退款没有发生”。

WAITING_FOR_APPROVAL 的过期方案由 worker 的小型维护扫描处理，建议每 5 秒扫描一批；多个副本通过条件更新与行锁保证一次失效。审批接口也执行相同检查。过期清理不长期占用 worker。

### 领取与锁顺序

建议使用短事务的行锁领取，跳过已锁定工作。PostgreSQL 的 SKIP LOCKED 可用于多消费者队列表，但租约和恢复逻辑仍由应用实现。[PostgreSQL SELECT 文档](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)

claim/heartbeat 事务只锁 job 并立即提交，不在持 job 锁时再写会话事件。凡同时修改公开状态、事件或业务动作的事务，统一锁顺序：

conversation → job → execution → proposal → policy → order → ticket。

缺少的行可跳过，同类多行按 ID 排序。审批和过期处理在 conversation 锁下读取最新 current_job_id；如果需要 job 锁，也遵循该顺序。模型和 HTTP 调用在数据库事务外进行。

所有 worker checkpoint、状态、工具结果写入必须匹配 job_id、worker_id、claim_version、RUNNING 和有效 lease；涉及 execution 时还须匹配 execution.current_job_id。重试原 job 不改变该指针，批准或内部恢复创建新 job 时必须同事务更新它。失去所有权后不能续写或生成新动作。公开 execution 的 QUEUED→RUNNING 在领取后用该检查单独提交，并同时写 execution.status_changed。

领取、续期、回收、审批有效期及取得全部锁后的资格检查使用实时 clock_timestamp()；now()/CURRENT_TIMESTAMP 固定为事务开始时间，不适合用来判断经历锁等待后的当前租约。created_at 等记录时间可以使用事务时间。[PostgreSQL 当前时间函数](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)

仅在 worker 调用前检查 lease 不够：旧 worker 可能暂停，等租约过期后仍发出请求。模拟服务也必须在自己的新动作事务中锁定权威行，验证当前 job/claim、execution stage、固定方案、唯一 APPROVE、版本、有效期与数据权限；取得全部锁后使用当前数据库时间再次检查 lease。

模拟服务新动作执行顺序：

1. 验证服务身份和 actor 对存储 proposal/execution 的归属，按 action_key 查已有结果；proposal_id 和 hash 一致则只读重放。重放免除当前 lease、有效期和已变化业务版本检查，但不免除访问权限检查。
2. 无结果时开启短事务，按统一顺序锁权威行。
3. 再次查 action_key，防止初次查询后的并发成功。
4. 检查 RUNNING、current_job_id、worker 与 claim_version、有效 lease、stage=APPLY_RESOLUTION、批准及固定方案。
5. 检查有效期、业务版本、目标关联和所有权。refund 非空时另检查当前资格和准确全额金额；refund=null 不要求 ELIGIBLE。
6. 原子保存回复、可选退款、工单变更及 action_result，再返回稳定结果。

成功动作后的只读重放不要求旧 lease 仍有效；但旧 worker 仍不能用该结果写应用 checkpoint，只有当前 owner 可以收尾。

### 必须原子提交的事务

| 边界 | 一起提交的内容 |
| --- | --- |
| 受理消息 | USER message、初始 job/receipt、active_message_id、message.accepted |
| 普通回答或路由澄清 | 当前 owner 检查、唯一最终 ASSISTANT、PROCESS_MESSAGE job SUCCEEDED、清除匹配 active_message_id、message.created |
| 首次创建任务 | execution、job 关联、被接受工具调用、execution.created |
| 保存工具结果 | 当前 owner 检查、tool result、checkpoint、进度事件 |
| 暂停审批 | 不可变 proposal、checkpoint、WAITING_FOR_APPROVAL、job SUCCEEDED、proposal.created |
| 批准 | 唯一 approval、execution QUEUED、唯一恢复 job、新 current_job_id、审批/状态事件 |
| 只读任务结束 | 当前 owner 检查、结果及唯一最终 ASSISTANT、execution COMPLETED、当前 job SUCCEEDED、清除匹配占用、事件 |
| 拒绝或未执行失效 | 决定或失效记录、终态、最终消息、当前活动 job 若存在则 SUCCEEDED、清除匹配占用、事件 |
| 模拟处置 | 权威检查、reply、可选 refund、业务版本变化、action_result |
| 应用收尾 | 确认的动作结果、最终 ASSISTANT message、execution COMPLETED、job SUCCEEDED、释放占用、事件 |

API 不需要与模拟服务共享一个跨 HTTP 的事务。模拟服务业务事务先完成，应用 checkpoint 后保存；稳定 action_key 专门处理这两步之间的崩溃。

### 结果未知与内部对账

APPLY_RESOLUTION 超时后不能假定失败。重试先查同 action_key；已成功则只进入 FINALIZE。收到可靠的“新动作验证失败”时也先确认没有既有结果，才可失效或失败。

自动尝试耗尽但结果仍未知时，保存 RECOVERY_REQUIRED、reason=ACTION_OUTCOME_UNKNOWN，当前 job FAILED 并撤销 lease/递增 claim_version。界面显示“执行结果尚未确认，任务已暂停对账”，保留会话占用。

如果动作结果已确认，但保存最终消息等收尾失败，同样进入 RECOVERY_REQUIRED、reason=FINALIZATION_FAILED；界面应显示“模拟变更已应用，任务收尾尚未完成”。不能显示退款未发生。

第一版内部维护步骤在服务恢复后：

1. 按统一锁顺序取得权威锁，条件检查 execution.status=RECOVERY_REQUIRED 且 current_job_id 仍指向本次对账的 FAILED job。建立屏障，确保旧调用已经提交、回滚，或之后因旧 claim 被拒绝。
2. 查 business.action_results 的 key/hash。已有成功结果则保存结果，并安排只执行 FINALIZE 的恢复 job。
3. 没有结果且屏障证明旧调用不可能再提交时，检查获批方案。仍有效则按同一 action_key 安排 APPLY_RESOLUTION 恢复 job；已过期或确定业务条件失效则进入 INVALIDATED、保存最终消息并释放占用。
4. 安排新 job 时，在同一事务递增 recovery_generation、创建唯一 recovery queue_key、更新 current_job_id、设 QUEUED 并写事件。并发维护看到状态或指针已改变时返回已有安排，不另建 job。
5. 锁超时或无法可靠确认则保持 RECOVERY_REQUIRED，不猜测结果、不换 key。

该步骤可作为内部管理命令实现并进入恢复演练；不新增用户恢复、取消或任意重跑接口。维护对账默认只能读结果；需要再次安排执行时仍使用受约束的恢复事务。

### 可调参数建议

| 参数 | 起始值 | 约束 |
| --- | --- | --- |
| worker lease | 60 秒 | 使用数据库时间 |
| heartbeat | 每 15 秒 | 不能使过期 owner 重新获得所有权 |
| 每个 job 尝试 | 最多 3 次，含首次 | 重领计数递增；批准恢复 job 单独计数 |
| 重试延迟 | 2 秒、10 秒 | 只重试暂时网络/服务错误；结果未知遵循对账规则 |
| 模型调用超时 | 30 秒 | 请求前持久增加调用计数 |
| 业务调用超时 | 10 秒 | 写调用超时不等于未写入 |
| 模型上下文 | 最近 20 条消息，整体输入最多 8000 tokens | 额外系统指令与工具结果也计入；超出时裁剪较旧上下文 |
| 每个任务模型调用 | 最多 8 次，含路由 | 重试不重置预算；普通聊天计数保存在 job |
| 每个任务工具调用 | 最多 12 次 | 已接受步骤和重试次数都受预算限制 |
| SSE 查询 / 心跳 | 1 秒 / 15 秒 | 可经实验调整 |
| 方案有效期 | 最多 24 小时 | 退款窗口可能更早结束 |

租约、轮询和上下文值可调整；审批对象、事务边界、幂等含义和结果未知的处理不能因调参改变。

## 验收与实施顺序

### 第一版验收案例

| 案例 | 必须观察到的结果 |
| --- | --- |
| 普通知识聊天 | 无 execution、无业务写入；消息与回答持久保存 |
| 只检查订单 1001 | 创建只读 execution，结果准确，无 proposal 和退款 |
| 目标不明 | 澄清后结束；不猜工单/订单 |
| 工单 393 缺信息 | NEEDS_INFORMATION；没有虚构交付日期 |
| 准备工单 391 | 固定全额方案，WAITING_FOR_APPROVAL，无业务写入，worker 可处理其他会话 |
| 批准 / 拒绝 | 批准后一次执行；拒绝后没有任何业务变更 |
| 不退款的回复与关闭 | 同样先保存方案并审批 |
| 30 天边界 | 恰好 30 天可退款，超过 1 秒不可退款 |
| 首次审批过期 / 获批后业务变化 | INVALIDATED，不能执行改过的方案 |
| 相同 key 相同消息重发 | 相同 receipt，不新增 message/job |
| 相同 key 不同内容 | 409 IDEMPOTENCY_CONFLICT |
| 重复审批 / approve-reject 竞争 | 唯一决定，最多一个批准恢复 job |
| 活动会话新消息 | 409 CONVERSATION_BUSY；其他会话可继续 |
| 同一工单两个处置，或同一订单两个退款提案 | 最多一个有效处置，无第二次退款或重复回复 |
| 同一订单、不同工单的两个无退款处置 | 都可独立获批完成，不把订单锁误当成唯一处置约束 |
| 杀 worker 后接管 | 从持久 checkpoint 恢复；旧 claim 写入和新动作被拒绝 |
| 动作成功后、checkpoint 前杀 worker | 重放原 action_result，一次退款、一次回复 |
| 写动作结果未知或收尾失败 | RECOVERY_REQUIRED 显示准确；内部对账后恢复 |
| SSE 快照及跨 API 重连 | 无漏事件；重复事件去重；不重新执行任务 |
| Alice 访问或批准 Bob 资源 | 无泄露、无业务写入 |
| 容器替换和备份恢复 | 消息、审批、checkpoint、业务动作与幂等记录均保留 |

先使用确定性模型 stub、固定时钟与独立 fixtures 验证，再单独测试真实模型。真实模型和 stub 模式应在演示中明确区分。

### 推荐开发拆分

1. API/数据负责人先实现 users、sessions、conversations、messages、jobs、events、消息受理与历史查询；单 worker 返回固定回答。CI 从这一阶段构建和检查，尽早验证最小云端部署。
2. 业务负责人提供样例数据、两个只读工具和固定资格规则；执行负责人接入 execution、结构化模型 stub 与 checkpoint。
3. 接入 proposal、approval、恢复 job 和原子模拟处置，立即验证重复批准及成功后崩溃。
4. 验证旧 worker、多个 worker、对账、SSE 回放与跨 API 副本。
5. 接真实模型，补齐已有计划中的 HTTPS、权限测试、监控、备份恢复、完整交付流水线及回滚演练。

上述是工作范围，不是已经分配给具体成员的职责。schema 迁移、API/工具字段和状态规则的变更应有一个协调负责人，合并前同步相关模块。

## 待团队确认的建议

| 建议决定 | 采用理由 | 改动影响 |
| --- | --- | --- |
| CAD、交付后 30 天、全额一次退款 | 政策可解释、可确定性测试 | 改政策、fixtures、资格和金额校验 |
| 用户级所有权，发起者审批自己任务 | 避免第一版引入组织与角色系统 | 独立审批员需要新权限规则 |
| 会话在等待审批时也 busy | 避免上下文并发和运行任务编辑 | 允许并行聊天需要新的路由与上下文隔离 |
| 一份方案对应一次原子 resolution | 消除部分成功工作流 | 拆分动作需逐项恢复和结果表达 |
| 单 execution 一份不可变方案 | 简化审批版本和失效处理 | 支持编辑要新增版本及重新审批规则 |
| stale/expired 使用 INVALIDATED 终态 | 旧批准不扩大为新授权 | 用户重新提交任务；以后可加重新提案 |
| 结果未知用 RECOVERY_REQUIRED | 避免把已执行动作误报为未执行 | 需内部对账步骤，但不新增用户工作流 |
| 同源 session cookie 加 CSRF | 便于普通请求与 SSE 使用同一身份 | 跨域部署需重新设计 cookie、CORS 和认证 |
| 同一 PostgreSQL、独立 app/business schema | 保留服务边界并方便权威锁与原子模拟 | 分开数据库会改变授权核验与恢复方案 |

具体模型、密码哈希库、迁移工具、云机器规格、反向代理、镜像仓库和备份调度器可在实现与实验阶段选择。实际成员、负责人、云访问和费用上限由团队记录；不能从四个工作范围推断团队人数。
