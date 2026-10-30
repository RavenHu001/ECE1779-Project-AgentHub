# 当前后端技术栈

当前已实现的后端使用 **Python、FastAPI、PostgreSQL 和 Docker Compose**。它验证账号、接收消息、保存持久待办，再由独立 worker 返回固定英文回复。下面说明各项技术在这个起步版本中的作用和适用原因。

源文件职责见 [代码结构说明](CODE_STRUCTURE.md)，启动命令见 [中文使用说明](README.md)。面向评审人的 [英文版](../../TECH_STACK.md) 与本文对应。

## 应用与数据库

| 技术与配置版本 | 在项目中的作用 | 适用原因 |
| --- | --- | --- |
| Python 3.13 | 编写 API、worker、账号命令和测试 | 共用一种语言，方便各进程复用模型、配置和数据库代码 |
| FastAPI 0.162.0 | 定义 HTTP 接口、身份验证依赖和 OpenAPI 契约 | 类型化接口和自动生成的接口说明，便于前端对接和手动测试 |
| Uvicorn 0.54.0 | 运行 FastAPI 应用的 ASGI 服务器 | 提供明确的服务器启动入口，可以放进共用运行镜像 |
| Pydantic 2.14.2 | 校验输入，定义响应结构 | 集中检查 UUID 格式、非空文本等规则，并控制接口返回哪些字段 |
| PostgreSQL 17 | 保存账号、会话、消息和待办 | 用一个数据库中的事务、约束和行锁支持数据持久化与 worker 协调领取 |
| SQLAlchemy 2.0.55 | 将表映射为 Python 类，组织查询、会话和事务 | API 与 worker 可以共用数据定义，并明确控制事务边界 |
| psycopg 3.3.6 与 psycopg-binary 3.3.6 | SQLAlchemy 底层使用的 PostgreSQL 驱动 | 负责 Python 与 PostgreSQL 通信；二进制包使镜像无需从源码编译驱动 |
| Alembic 1.20.1 | 执行版本化的数据库结构变更 | 将迁移纳入 Git，让队员和联调环境按同一版本顺序升级 |

FastAPI 组织请求处理，Uvicorn 提供服务器；Pydantic 检查接口数据，SQLAlchemy 组织数据库操作，psycopg 实际与 PostgreSQL 通信。这几层分工互相配合。

对应文件是 [main.py](../../../backend/app/main.py)、[schemas.py](../../../backend/app/schemas.py)、[models.py](../../../backend/app/models.py)、[db.py](../../../backend/app/db.py) 和 [迁移目录](../../../backend/migrations/)。

## 后台处理与身份验证

| 选择 | 作用 | 适用原因与当前边界 |
| --- | --- | --- |
| 独立 Python worker 与 PostgreSQL 待办队列 | 领取工作，保存回复，记录完成或失败 | 复用已有语言和数据库。行锁、到期时间和每次更新的领取令牌支持中断后的接管；起步阶段无需增加独立消息代理 |
| HTTP Basic | 对接口请求进行开发账号验证 | 可直接用于 Swagger 和 Postman，在完整登录会话流程建立前支持独立账号。应通过 SSH 隧道或 HTTPS 使用；Basic 本身不加密凭据 |
| 带随机盐的 scrypt | 通过 Python 标准库处理密码哈希 | 保存哈希而不是明文，初始镜像无需再增加密码哈希依赖包 |

当前 worker 返回 `Message received.`，尚未实现长任务续期和外部业务操作。HTTP Basic 是当前开发阶段采用的机制，不代表最终登录方案已经确定。

对应实现见 [worker.py](../../../backend/app/worker.py)、[security.py](../../../backend/app/security.py) 和 [manage.py](../../../backend/app/manage.py)。

## 容器与团队交付

| 技术 | 作用 | 适用原因 |
| --- | --- | --- |
| Docker | 构建运行镜像和独立测试镜像 | 将 Python 环境与代码一起打包，使队员和 CI 使用同一套应用运行配置 |
| Docker Compose | 连接 API、worker、数据库、迁移和账号初始化服务 | 用共用配置声明启动顺序、网络、数据卷和隔离项目名，可用于个人环境或共享服务器 |
| Git | 管理代码、迁移、依赖和文档版本 | 通过分支和经过审阅的提交协调多人修改 |
| GitHub Actions | 构建容器、检查代码、运行数据库测试并验证真实链路 | 在集成前为 PR 提供共同验证流程；当前尚未配置远程自动部署 |
| uv 与 pip | uv 生成解析后的依赖文件；pip 在镜像构建时安装 | 维护者集中解析版本，Docker 安装审阅后的清单。uv 是维护工具，不是运行时服务 |

项目要求 Docker Engine 已运行，并使用支持 `--wait` 的 Compose v2 或以上版本。应用没有固定宿主机 Docker 和 uv 的版本。基础镜像为 `python:3.13-slim-bookworm`、`postgres:17-bookworm`；这些标签限定版本系列，并不是固定镜像摘要。

配置位于 [compose.yaml](../../../compose.yaml)、[Dockerfile](../../../backend/Dockerfile)、[.env.example](../../../.env.example) 和 [CI 流程](../../../.github/workflows/ci.yml)。真实凭据保存在被 Git 忽略的 `.env` 中。

## 测试与代码质量

| 技术与锁定版本 | 作用 | 适用原因 |
| --- | --- | --- |
| pytest 9.2.0 | 执行集成测试和公共准备步骤 | 可重复验证请求处理、数据归属、事务回滚和 worker 恢复 |
| HTTPX 0.28.1 | 为 FastAPI 的 HTTP 测试客户端提供支持 | 在自动测试中验证接口请求、响应和身份验证行为 |
| Ruff 0.17.3 | 检查常见问题、导入顺序和代码格式 | 通过同一套规则减少队员与 CI 之间的代码风格差异 |
| FastAPI 提供的 Swagger UI | 在 `/docs` 进行交互式手动测试 | 队员和评审人可以查看请求字段并直接试用接口 |
| Postman | 可选的外部手动测试客户端 | 方便明确设置身份验证和重复发送请求；它不是应用依赖 |

自动测试使用独立的临时 PostgreSQL 数据库，不清理共享开发数据。[smoke.py](../../../backend/app/smoke.py) 则访问实际运行的 API，等待独立 worker 返回结果。手动操作与结果记录见 [测试指南](MANUAL_TESTING.md)。

## 语言与文件格式

| 语言或格式 | 用途 |
| --- | --- |
| Python | 应用逻辑、测试、环境初始化和迁移函数 |
| SQL | 建表、约束和查询；很多查询由 SQLAlchemy 生成 |
| YAML | Compose 服务定义和 GitHub Actions 流程 |
| TOML | `pyproject.toml` 中的 pytest、Ruff 配置 |
| Markdown | 评审与开发者文档 |
| Mako | Alembic 新迁移文件的模板，不是前端页面模板 |

系统提示和评审文档使用英文，代码注释使用中英双语；中文说明集中在 `docs/developer/zh-CN/`。合法的 Unicode 用户输入保持原样。

## 版本维护与后续规划

上面的 Python 包版本来自已提交的 [运行依赖](../../../backend/requirements.txt) 和 [开发依赖](../../../backend/requirements-dev.txt)，描述的是本仓库当前配置，不表示推荐追逐最新版本。更新依赖时同步维护 `.in` 和重新生成的 `.txt`，重新构建、测试后再提交。

间接依赖也记录在锁定文件中，但安装了某个库不等于配置了它的所有功能。例如存在 OpenTelemetry 依赖，并不代表当前已经实现链路追踪系统。

React、模型 API、审批、SSE、模拟业务服务、正式 Swarm 部署、云端监控和自动备份仍是后续规划。当前也没有 Redis、Celery 或独立消息代理服务。完整方向见 [英文架构交接文档](../../AGENTHUB_TEAM_HANDOVER.md)。

官方参考：[FastAPI 功能说明](https://fastapi.tiangolo.com/features/)、[SQLAlchemy 会话](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)、[Docker Compose 应用模型](https://docs.docker.com/compose/intro/compose-application-model/)。
