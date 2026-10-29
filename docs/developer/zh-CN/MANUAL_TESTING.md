# 后端手动测试指南

使用 Swagger 接口页面或 Postman，手动检查当前 Docker 后端的账号验证、聊天、持久待办和固定英文回复。模型调用、审批、SSE 和退款尚不属于这套测试。

启动细节见 [中文使用说明](README.md)。[英文版](../../MANUAL_TESTING.md) 使用相同的测试编号和结果字段，方便队员与评审人对照。

## 准备测试环境

以下命令在仓库根目录执行。新建的独立环境先运行 `python scripts/init_env.py`（Linux 按需使用 `python3`），再启动服务：

```bash
docker compose up -d --build --wait
docker compose ps -a
```

使用已经运行的共享环境时，直接访问维护者分配的地址，并向维护者确认服务状态，不要自行执行启动或更新命令。

将分配给你的账号称为 **账号 A**。如果是自己首次启动的环境，私下查看 `.env` 中的 `BOOTSTRAP_USERNAME` 和 `BOOTSTRAP_PASSWORD`。不要把密码、身份验证请求头或 `.env` 内容放进测试截图和记录。

打开 <http://127.0.0.1:8000/docs>。如果服务运行在远程服务器上，先建立 SSH 隧道：

```bash
ssh -N -L 18000:127.0.0.1:8000 your-user@your-server
```

然后打开 <http://127.0.0.1:18000/docs>；Postman 也使用相同主机和端口作为基础地址。在 Swagger 中点击 **Authorize**，填写账号 A 的凭据；展开接口，使用 **Try it out → Execute**。Postman 选择 **Basic Auth**。

MT01–MT13 主要通过接口执行，会产生测试记录；MT13 还需要第二个账号。**MT14 和 MT15 会停止或重建服务，应在自己的独立单 worker 环境执行。在共享服务器上进行类似操作前，需要先与维护者协调。** 条件不满足时记录为 `BLOCKED`，不要直接停止大家共用的服务。

每次真正提交一条新消息，都生成新的 UUID：

```powershell
[guid]::NewGuid()
```

也可以使用：

```bash
python -c "import uuid; print(uuid.uuid4())"
```

测试中保存以下编号：

| 名称 | 需要记录的内容 |
| --- | --- |
| `C1` | MT03 中账号 A 创建的会话；后续继续使用这个会话 |
| `K1` | MT05 使用的提交标识 |
| `M1` | MT05 返回的用户消息编号 |
| `J1` | MT05 返回的待办编号 |
| `J2` | MT14 停止 worker 后创建的待办编号 |

路径参数填写真实 UUID。`C1`、`M1`、`J1` 只是便于说明的名称，不能直接当作 UUID 输入。

## 正常流程

### MT01 服务启动与健康检查

查看 `docker compose ps -a`，然后不带身份验证调用 `GET /health`。

预期：数据库、API 和 worker 正在运行，API 健康；`migrate`、`bootstrap` 以退出码 0 结束。接口返回 HTTP `200`：

```json
{"status": "ok", "database": "ok", "reply_mode": "fixed"}
```

### MT02 账号验证

使用账号 A 调用 `GET /me`，预期 HTTP `200`，包含对应账号编号和用户名，不包含密码哈希。换成错误密码重试，预期 `401`，然后恢复正确凭据。

### MT03 创建会话

调用 `POST /conversations`：

```json
{"title": "Manual backend test"}
```

预期 HTTP `201`，返回标题和会话编号。将这个会话编号记录为 `C1`。

### MT04 英文默认标题

再次调用 `POST /conversations`，请求体为 `{}`。预期 HTTP `201`，标题为 `New conversation`。这会额外创建一个会话；后续仍使用最初的 `C1`。

### MT05 提交消息

调用 `POST /conversations/{C1}/messages`，使用一个新的 UUID 作为提交标识：

```json
{
  "content": "Hello",
  "submission_key": "cbd2fe4d-3781-4856-b416-44242a730dac"
}
```

预期 HTTP `202`，返回 `message_id`、`job_id`。将提交标识记为 `K1`，两个返回编号分别记为 `M1`、`J1`。受理成功不等于处理已经完成。

### MT06 查询回复与工作状态

调用 `GET /conversations/{C1}/messages` 和 `GET /jobs/{J1}`。在默认配置下，可以刷新等待最多 30 秒。

预期：出现用户消息 `Hello` 和一条系统回复 `Message received.`，顺序号分别为 1、2；回复的 `reply_to_message_id` 等于 `M1`。`J1` 最终变成 `SUCCEEDED`。worker 很快时，可能观察不到中间的 `QUEUED`、`RUNNING` 状态。

## 重复提交与错误输入

### MT07 重复提交相同请求

保持 `C1`、`K1`、正文 `Hello` 完全不变，重复 MT05。预期 HTTP `202`，返回原来的 `M1` 和 `J1`；聊天里仍只有原请求及其回复，不新增重复记录。

### MT08 同一标识提交不同正文

继续使用 `K1`，把正文改成 `Different text`。预期 HTTP `409`，已有聊天记录不变。

### MT09 拒绝空白正文

使用新的 UUID，先将 `content` 设为 `""`，再改为 `"   "` 测试。每次预期 HTTP `422`，不新增消息。这属于接口输入校验失败，不是 worker 处理失败。

### MT10 查询不存在的会话

生成一个格式正确、但没有用来创建过会话的 UUID，调用 `GET /conversations/{该UUID}/messages`。预期 HTTP `404`。如果 UUID 格式本身错误，会返回 `422`，那是另一种校验情况。

### MT11 保留 Unicode 用户输入

向 `C1` 提交一条新消息，使用新的提交标识：

```json
{
  "content": "你好",
  "submission_key": "5c3f0596-6c53-48be-bfd0-b9ca836fcf29"
}
```

预期：保存的用户消息仍为“你好”，系统回复仍为 `Message received.`。此时聊天已有新增记录，不再要求总消息数等于两条。

## 身份验证与数据归属

### MT12 拒绝没有凭据的请求

在 Postman 新建 `GET /me` 请求，选择 **No Auth**，确认没有 `Authorization` 请求头。预期 HTTP `401`。

浏览器可能缓存并重复发送 HTTP Basic 凭据，因此仅在 Swagger 中退出授权，不能证明请求没有凭据。测试后恢复账号 A 的身份验证。

### MT13 两个账号之间的数据隔离

取得另一个独立的 **账号 B**。维护者可以创建测试账号：

```bash
docker compose run --rm admin create-user manual-b
```

按提示输入新密码；账号已经存在时使用分配好的账号，不要重置其他测试者的密码。

切换为 B，分别查询 `GET /conversations/{C1}/messages`、使用合法消息输入调用 `POST /conversations/{C1}/messages`，以及查询 `GET /jobs/{J1}`，预期全部返回 `404`。B 的 `GET /conversations` 列表不能包含 `C1`；B 已有其他会话时，列表不一定为空。

后续继续测试前，切回账号 A，确认仍能访问 `C1`。

## 服务中断与数据保存

### MT14 Worker 停止期间仍可保存待办

在自己的独立单 worker 环境执行：

```bash
docker compose stop worker
```

向 `C1` 提交新消息，使用新的提交标识，把返回的待办编号记为 `J2`。预期提交返回 HTTP `202`；用户消息可以查到，`J2` 保持 `QUEUED`，这条新消息暂时没有回复。

启动 worker：

```bash
docker compose up -d worker
```

默认配置下，预期 30 秒内 `J2` 变成 `SUCCEEDED`，并且只有一条回复关联这条来源消息。测试后保持 worker 运行。本项验证的是未领取待办能保存下来，不代表已经手动验证“处理中崩溃后接管”。

### MT15 容器重建后记录仍然存在

记下 `C1` 当前消息的编号、正文，以及 `J1` 的完成状态。保持同一仓库目录、`.env` 和 Compose 项目名，执行：

```bash
docker compose down
docker compose up -d --wait
```

重新使用 A 验证身份，再查询 `C1` 和 `J1`。预期消息编号、正文仍保留，`J1` 仍为 `SUCCEEDED`，初始化过程没有重置账号密码。

**不要添加 `-v` 或 `--volumes`，它们会删除数据库数据卷。** 本项也不等于备份恢复测试。

## 失败时怎样排查

记录测试编号、HTTP 状态、去除敏感信息的响应、相关资源编号和发生时间。查看失败 job 的 `last_error`，并检查日志：

```bash
docker compose logs --tail 100 api worker
docker compose logs --tail 100 migrate bootstrap db
```

待办一直排队时，先检查 worker 是否运行；请求返回 `503` 时，检查数据库是否可用。重试前先记录实际结果，不能因为后续成功，就把第一次失败当作没有发生。

## 测试结果记录表

每次手动测试复制本节填写。结果统一使用 `NOT_RUN`、`PASS`、`FAIL`、`BLOCKED`，分别表示未执行、通过、失败、受阻。执行后再填写结果，之前的自动化测试结果不能代填这份手动记录。

| 本轮信息 | 填写内容 |
| --- | --- |
| 测试人 | 待填写 |
| 日期时间及所在时区 | 待填写 |
| 被测提交，使用 `git rev-parse --short HEAD` 获取 | 待填写 |
| API 基础地址及环境名称 | 待填写 |
| Compose 项目名和 worker 数量 | 待填写 |
| 测试工具及版本 | 待填写 |
| 账号 A、B 的用户名，不记录密码 | 待填写 |
| 相关配置覆盖值 | 待填写，不含秘密 |

| 编号 | 测试内容 | 预期结果 | 实际结果与证据位置 | 结果 |
| --- | --- | --- | --- | --- |
| MT01 | 启动与健康检查 | 服务正常；HTTP 200 | 待填写 | NOT_RUN |
| MT02 | 正确和错误凭据 | HTTP 200 和 401 | 待填写 | NOT_RUN |
| MT03 | 创建会话 | HTTP 201；记录 C1 | 待填写 | NOT_RUN |
| MT04 | 默认标题 | New conversation | 待填写 | NOT_RUN |
| MT05 | 提交消息 | HTTP 202；记录 M1、J1 | 待填写 | NOT_RUN |
| MT06 | 查询回复和待办 | 一条关联的英文回复；SUCCEEDED | 待填写 | NOT_RUN |
| MT07 | 重复提交 | 编号相同；不新增记录 | 待填写 | NOT_RUN |
| MT08 | 标识与正文冲突 | HTTP 409；历史不变 | 待填写 | NOT_RUN |
| MT09 | 空白正文 | HTTP 422；不新增消息 | 待填写 | NOT_RUN |
| MT10 | 不存在的会话 | 有效但不存在的 UUID 返回 404 | 待填写 | NOT_RUN |
| MT11 | Unicode 用户输入 | 原文保留；英文回复 | 待填写 | NOT_RUN |
| MT12 | 没有身份凭据 | HTTP 401 | 待填写 | NOT_RUN |
| MT13 | 其他账号的资源 | HTTP 404；B 列表不含 C1 | 待填写 | NOT_RUN |
| MT14 | 停止 worker | 先 QUEUED，再一条回复及 SUCCEEDED | 待填写 | NOT_RUN |
| MT15 | 重建容器 | 已存记录和账号访问均保留 | 待填写 | NOT_RUN |

| 后续信息 | 填写内容 |
| --- | --- |
| 汇总 | PASS：__ / FAIL：__ / BLOCKED：__ / NOT_RUN：__；合计 15 项 |
| 问题链接与复现步骤 | 待填写 |
| 复测日期与结果 | 待填写 |
| 环境恢复确认 | 待填写：确认 worker 运行，检查待办及服务健康状态 |

证据可以使用脱敏截图、响应片段或仓库 Issue 链接。保留首次失败记录，追加复测结果，不要覆盖。共享环境中的测试数据按维护者约定处理，不要为了清除测试会话而重置数据库。
