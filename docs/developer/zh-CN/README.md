# AgentHub 团队后端开发中文说明

面向评审人的英文入口见 [仓库 README](../../../README.md)。本文是队员使用的中文说明；运行时系统提示统一为英文。

逐项手动验证和结果记录见 [后端手动测试指南](MANUAL_TESTING.md)。

ECE1779 云计算课程团队项目。当前后端已经提供可容器化的起步链路：创建会话、提交消息、保存 PostgreSQL 待办、独立 worker 返回固定回复、查询聊天及工作状态。

**队员不需要安装 Python 项目的依赖或手动建表。** 运行所需依赖在镜像中；Compose 依次等待数据库就绪、执行迁移、初始化账号，然后启动 API 和 worker。当前回复是固定文字，不调用模型，也不需要模型 API 密钥。

## 团队如何协作

共同使用这个 Git 仓库，分别在分支开发，通过 PR 审阅后合并。GitHub Actions 会构建容器、检查代码、在独立 PostgreSQL 测试库运行测试，并验证 API 与 worker 的真实 HTTP 链路。

团队可以将同一份仓库部署在一台共享开发服务器，前端与其他队员通过统一 API 联调。启动命令与下面相同；个人也可以在自己的机器或远程开发环境运行独立副本。**共享环境由一名维护者更新，队员不要同时在它的工作目录切换分支或修改数据库。**

```text
队员分支 → PR 自动检查 → 审阅后合并 → 维护者更新共享服务器 → 团队联调
```

当前提供的是开发和早期联调环境。仓库内的 CI 不会自动发布镜像或登录云服务器；共享服务器需要维护者按下文部署。正式 Swarm 部署、HTTPS 入口和部署凭据将在云端交付阶段接入。

## 首次启动

需要 Git、正在运行的 Docker Engine，以及支持 `--wait` 的 Docker Compose v2 或以上版本。初始化配置的小脚本需要 Python 3.10 或以上；它只使用标准库。Windows 使用 `python`，Linux 通常使用 `python3`。

在自己的开发环境或共享 Linux 服务器上执行：

```bash
git clone https://github.com/RavenHu001/ECE1779-Project-AgentHub.git
cd ECE1779-Project-AgentHub
python scripts/init_env.py
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps -a
```

Linux 请将初始化命令的 `python` 换成 `python3`。已克隆仓库的队员从仓库根目录开始，不要再次克隆。

初始化脚本会创建被 Git 忽略的 `.env`，随机生成数据库密码和初始账号密码；存在 `.env` 时不会覆盖。请用编辑器私下查看其中的 `BOOTSTRAP_USERNAME` 和 `BOOTSTRAP_PASSWORD`。密码不应粘贴到 PR、截图或聊天记录中。

启动成功后：

- 健康检查：<http://127.0.0.1:8000/health>。
- 交互式接口说明：<http://127.0.0.1:8000/docs>。
- `migrate` 和 `bootstrap` 显示 `Exited (0)` 是正常的，它们只负责一次性初始化。
- `api` 应显示健康，`worker` 保持运行；数据库不向宿主机开放端口。

`.env.example` 是配置说明模板，密码占位内容不能直接使用。也可以在缺少宿主 Python 时复制该模板为 `.env`，手动将两个密码占位替换为各自足够长的随机密码，再启动容器。

## 共享服务器与队员访问

默认只把 API 绑定到服务器的 `127.0.0.1`。因此，在远程服务器启动后，队员可通过 SSH 隧道访问，不必把开发接口直接暴露到公网：

```bash
ssh -N -L 18000:127.0.0.1:8000 your-user@your-server
```

保持隧道窗口运行，然后在队员自己的浏览器打开 <http://127.0.0.1:18000/docs>，或在 Postman 中使用 `http://127.0.0.1:18000` 作为 API 地址。服务器地址和 SSH 访问由团队维护者分配。

当前接口采用 HTTP Basic 账号验证，密码在数据库中以 scrypt 哈希保存。请通过 SSH 隧道或 HTTPS 访问。需要统一 HTTPS 地址时，由云端维护者配置反向代理和证书，再决定是否调整 `.env` 中的 `API_BIND_HOST`；仅修改监听地址不会自动提供 HTTPS。正式登录会话机制仍可在后续阶段调整。

为队员建立各自账号，不必共享初始账号：

```bash
docker compose run --rm admin create-user alice
docker compose run --rm admin create-user bob
```

命令会交互式询问密码并要求确认，不会写进命令历史。用户名使用 1–64 位英文字母、数字、点、横线或下划线，并以字母或数字开头；保存时统一为小写。当前 HTTP Basic 凭据使用 ASCII 字符，密码至少 12 位。每个账号只能查询和操作自己的会话、消息与待办。

修改已有账号密码：

```bash
docker compose run --rm admin set-password alice
```

修改 `.env` 不会重置数据库中的已有账号密码。`bootstrap` 重复执行时保留已有账号；若修改初始账号密码，需要同时维护 `.env` 中用于冒烟检查的初始账号凭据。

## 第一次调用接口

在 `/docs` 页面点击 **Authorize**，填写分配给你的账号和密码；在 Postman 中选择 **Authorization → Basic Auth**。除了 `/health`，下面的业务接口都需要账号验证。

### 创建会话

调用 `POST /conversations`，请求体：

```json
{"title": "我的第一次后端测试"}
```

返回 `201`，记下响应的 `id`，作为下一步的会话编号。

### 发送消息

调用 `POST /conversations/{会话编号}/messages`：

```json
{
  "content": "你好",
  "submission_key": "cbd2fe4d-3781-4856-b416-44242a730dac"
}
```

返回 `202`，包含 `message_id` 和 `job_id`，表示已保存并排队。请求超时重发时保持相同的 `submission_key`；新的消息要换一个 UUID。同一标识配上不同正文会返回 `409`。

Windows PowerShell 可用 `[guid]::NewGuid()` 生成新 UUID；有 Python 时可用 `python -c "import uuid; print(uuid.uuid4())"`。

### 查询回复和处理状态

调用 `GET /conversations/{会话编号}/messages`。worker 默认每秒查看待办，稍后可以看到用户的“你好”和系统的“Message received.”。回复通过 `reply_to_message_id` 指向原消息。

调用 `GET /jobs/{job_id}` 查看 `QUEUED`、`RUNNING`、`SUCCEEDED` 或 `FAILED`。这有助于区分“仍在等待”和“已经处理失败”。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 检查 API 与数据库连接，返回当前固定回复模式 |
| `GET /me` | 查看当前账号资料 |
| `POST /conversations` | 创建会话 |
| `GET /conversations` | 列出自己的会话，默认最多 100 条 |
| `POST /conversations/{id}/messages` | 保存消息和待办；相同提交返回原编号 |
| `GET /conversations/{id}/messages` | 按 `seq` 排序读取消息；支持 `after_seq`、`limit` 分页 |
| `GET /jobs/{id}` | 查看自己消息的处理状态 |

两项列表接口的 `limit` 范围为 1–500；会话列表按最近更新时间排序。访问别人的资源返回 `404`，缺少或错误的账号凭据返回 `401`，输入不合法返回 `422`，数据库暂不可用返回 `503`。

## 验证运行与数据保存

维护者可以直接运行完整容器链路检查，无须把密码复制到命令中：

```bash
docker compose exec -T api python -m app.smoke
```

它会创建一个测试会话，提交消息、重复提交、等待独立 worker 回复，并打印 `conversation_id`。如果初始账号凭据已经变更，请先同步 `.env` 并重建使用该环境变量的服务。

检查重建后数据是否保留时，记下这个编号，然后执行：

```bash
docker compose down
docker compose up -d --wait
docker compose exec -T api python -m app.smoke --verify-conversation 替换为刚才的会话UUID
```

普通 `down` 不删除数据库持久卷。**不要在共享环境使用 `down --volumes` 或 `down -v`，它们会删除该环境的数据库数据。** 持久卷也不代替后续需要建立的备份。

## 开发修改与测试

修改代码后重新构建并启动，使镜像里的代码更新：

```bash
docker compose up -d --build --wait
docker compose logs --tail 100 api worker
```

API 和 worker 使用同一份运行镜像、不同启动命令。当前不把宿主代码目录挂进容器，因此修改文件后需要重建。worker 只能产生固定回复；不会执行退款、业务工具或模型调用。

运行检查与测试：

```bash
docker compose --profile test build test
docker compose run --rm --no-deps test sh -c "ruff check . && ruff format --check ."
docker compose run --rm test
```

测试使用独立的 `test-db` 容器和 `agenthub_test` 数据库，测试数据可清空；不连接开发使用的 `db`。测试还检查数据库名称必须以 `_test` 结尾。`test-db` 使用临时内存挂载，容器停止后不保留数据。

数据库集成测试覆盖消息受理与回复、重复请求、用户隔离、并发领取、过期领取接管、旧 worker 写入被拒绝，以及迁移与模型一致性。固定回复很短，本阶段不实现长任务续期；领取默认 60 秒，因进程中断而过期的工作最多领取 3 次，处理异常直接记录为失败。

停止临时测试数据库，保留正常开发环境：

```bash
docker compose stop test-db
```

需要并行验证多个 worker 时：

```bash
docker compose up -d --scale worker=2 --wait
```

### 同一台服务器上的独立分支环境

队员应使用各自的仓库目录，并在每份目录生成不同的项目名和 API 端口，例如：

```bash
python scripts/init_env.py --project-name agenthub-alice --api-port 8001
docker compose up -d --build --wait
```

项目名决定容器、网络、数据卷和镜像的命名。不要给两个独立环境使用相同的 `COMPOSE_PROJECT_NAME`；不同端口则避免宿主端口冲突。数据库始终只在各自 Compose 网络内使用 `db:5432`。

## 数据库结构如何协作修改

四张表通过 Alembic 迁移 `0001_initial` 创建，迁移与应用代码一起进入 Git。迁移是独立的一次性服务，不会由每个 API 或 worker 副本各自执行。共享环境同一时间只运行一次更新流程。

后续修改表结构时：

1. 在自己的分支修改 `backend/app/models.py`。
2. 新建迁移文件，不修改已经交付使用的旧迁移。
3. 在隔离环境检查升级与模型一致性，提交模型和迁移供 PR 审阅。
4. 若其他队员也新增了迁移，先统一迁移链，再更新共享数据库。

维护者查看迁移状态：

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic heads
```

迁移生成环境需要能写回仓库，可在独立开发环境临时挂载 `backend` 目录：

```bash
docker compose run --rm --user 0 --volume "${PWD}/backend:/app" migrate alembic revision --autogenerate -m add_example
```

Linux 上以 root 生成的迁移文件可能需要调整文件归属。执行后必须检查生成内容，尤其是删除字段或数据转换；再通过 `docker compose up -d --build --wait` 使用新迁移。已有数据的共享环境不要通过降级或删卷来解决迁移问题。回退镜像也不会自动回退数据库结构。

依赖的范围写在 `requirements.in` 和 `requirements-dev.in`，具体版本锁定在相应 `.txt` 中。更新依赖的维护者可用 uv 按顺序重新生成锁定文件，再构建、测试并提交两套改动：

```bash
uv pip compile backend/requirements.in --python-version 3.13 --python-platform x86_64-unknown-linux-gnu -o backend/requirements.txt
uv pip compile backend/requirements-dev.in --python-version 3.13 --python-platform x86_64-unknown-linux-gnu -o backend/requirements-dev.txt
```

## 目录说明

```text
.github/workflows/ci.yml    PR 和 main 分支的容器检查与集成测试
.env.example               可提交的配置模板
compose.yaml               API、数据库、迁移、初始化、worker 和隔离测试环境
scripts/init_env.py         创建不提交的实际配置与随机密码
backend/Dockerfile          运行镜像和测试镜像
backend/app/                接口、账号管理、数据库模型和 worker
backend/migrations/         版本化数据库迁移
backend/tests/              PostgreSQL 集成测试
docs/                      总体规划、开发规划及字段设计
```

后端资料：[总体规划](AGENTHUB_BACKEND_PLAN.md)、[最早期开发规划](AGENTHUB_BACKEND_INITIAL_DEVELOPMENT_PLAN.md)、[四张数据表设计](AGENTHUB_BACKEND_INITIAL_DATABASE_DESIGN.md)。原规划中的模型、审批、SSE、模拟业务、云端 Swarm 与备份仍是后续工作。
