# 部署与配置

本文只描述 V2 单 Agent RAG 的本地部署和 Fixture 验收。规范性规则见 [V2 Spec](bookstore-single-agent-rag-refactor-spec.md)。真实模型、真实 Embedding、生产网络和成本不因本地 Fixture 通过而自动获得验收结论。

## 1. 默认 Compose

默认运行服务为：

```text
mysql                 业务事实、用户、订单、Trace 和 Outbox
redis                 缓存、限流和短期会话
qdrant                版本化 POLICY/BOOK_GUIDE 派生索引
mailpit               本地验证邮件
migrate/bootstrap     一次性初始化
bookstore-service     Go HTTP/BFF + 内部 gRPC
agent-api             Python FastAPI 单 Agent、RAG、SSE
agent-worker          MySQL Outbox 租约 Worker
nginx                 Vite 静态 SPA 和唯一公开入口
```

Prometheus、Grafana、Tempo、OTel Collector 仅在 `observability` Profile 中启动。Vite 的 Node.js 构建阶段不形成运行服务。内部 gRPC、Agent API、数据库和 Qdrant 不映射到宿主机。

```powershell
.\deploy\init-local.ps1
docker compose config --quiet
# 仅在该项目已有兼容 RAG Manifest 时：
docker compose up -d --build
docker compose ps -a
```

`.env` 已存在时初始化脚本拒绝覆盖；不要提交 `.env`、私钥或完整含秘密的 Compose 输出。应用启动只检查依赖和索引 Manifest，不在启动钩子中调用收费 Provider。 
空卷没有 Manifest 时不要执行上面的完整启动命令，转到下一节使用 Fixture 两阶段启动，或先按第 3 节手动建立索引。

## 2. Fixture 配置

Fixture Overlay 只替换 Chat/Embedding 网关，并显式设置测试 collection；业务服务、数据库、邮件、缓存、向量服务和浏览器入口仍是真实本地服务。空卷不能直接执行完整 `up`：`agent-api` 启动前必须已经存在 RAG Manifest。使用下面的两阶段启动器：

```powershell
.\deploy\start_v2_fixture.ps1 -Project bookstore-stage11
```

启动器只接受 `bookstore-stage11`、`bookstore-test...` 或 `bookstore-v2-fixture...` 项目名；它设置本地 Fixture 地址，先启动 MySQL/Redis/Qdrant/Mailpit、迁移、bootstrap、Go 服务和 `model-fixture`，再追加导入100本书，显式运行 RAG 全量索引，最后启动 `agent-api`、`agent-worker` 和 Nginx。它不执行 `down -v`、删除卷、清空表或输出密钥。重复执行沿用现有数据，导入按 ISBN 跳过已存在行。

如果 `.env` 是 V2 之前创建的，先按 `deploy/init-local.ps1` 输出补齐 `MYSQL_WORKER_PASSWORD`；不要把密码写入命令行或提交到 Git。

Fixture Overlay 使用专用固定测试网段；同一时刻只运行一个使用该网段的 Fixture 项目。启动器发现另一个项目占用该网段时会在创建容器前失败，不会停止或删除占用者。

Fixture 不访问外部 Provider，不代表真实模型推荐质量、RAG 质量或费用。测试只使用测试项目名；生产或真实模型项目不得叠加该 Overlay。

## 3. 两阶段知识索引

如需逐条执行而不使用启动器，首次空环境必须保持同样顺序。注意为启用 Fixture Overlay 必须带 `v2-app` Profile；Profile 中的三个应用服务在 Manifest 建立前不会被 `up` 拉起：

```powershell
$fixture = @('--profile', 'v2-app', '-p', 'bookstore-stage11', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml')
docker compose @fixture up -d --build mysql redis qdrant mailpit migrate bootstrap bookstore-service model-fixture
.\agent\.venv\Scripts\python.exe deploy/seed_manual_dataset.py --project bookstore-stage11 --apply
docker compose @fixture run --rm --no-deps agent-api python -m app.rag_index build --corpus all --fixture-parameters --chunk-target 500 --chunk-overlap 80 --policy-threshold 0.01 --book-threshold 0.01 --allow-provider-calls
docker compose @fixture up -d agent-api agent-worker nginx
```

`--allow-provider-calls` 是允许索引命令调用**当前配置的**Embedding端点的显式开关；在本Overlay中该端点是本地Fixture，不是真实供应商。命令里的 `500/80/0.01/0.01` 是冻结的流程测试参数，只验证索引、Manifest和服务启动，不能作为真实Embedding的Chunk或阈值调优证据。索引失败不得切换Alias；重复执行不得重置价格/库存或写入重复Point。

## 4. V2 Fixture smoke

```powershell
.\agent\.venv\Scripts\python.exe deploy/e2e_smoke.py --project bookstore-stage11
```

脚本会创建唯一测试用户和书籍，不删除已有用户或清空卷；覆盖：

- 注册、Mailpit 验证、登录、Session 和 HttpOnly Cookie；
- Catalog 查询、Guest Chat、Authenticated Chat 和 Trace；
- 订单预览、Go/Agent API 重启后的 MySQL 确认、幂等订单和库存扣减；
- 用户明确结构化偏好；
- 缺货监听、Agent Worker 重启、Outbox 消费和单条站内通知；
- Trace 时间线、`/api/evals` 与公开回放路径 404、Vite SPA 深链接。

脚本不运行真实模型、不打印密码/JWT/验证令牌、不使用直写订单或通知制造成功。真实页面可用性仍须由 Playwright Compose 用例和人工页面观察单独记录。

## 5. 可选观测验证

```powershell
$observed = @('--profile', 'v2-app', '--profile', 'observability', '-p', 'bookstore-stage11', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml')
docker compose @observed up -d
.\agent\.venv\Scripts\python.exe deploy/verify_telemetry.py --project bookstore-stage11
```

验证器只要求 V2 的 HTTP/Agent/工具/模型/业务服务技术链路、指标变化、Grafana 使用 Prometheus 和敏感字段过滤；默认不把观测 Profile 当作业务运行依赖。Prometheus 目标是 Go 业务服务和 Agent 服务，不能据此宣称真实模型质量。

## 6. 生产模型与 RAG 评测边界

真实模型或真实 Embedding 运行前必须明确授权，并记录：模型/Profile、Prompt 版本、Embedding 版本、Alias/Manifest、测试数据、调用次数、费用状态和 PASS/FAIL/BLOCKED/NOT_RUN。没有 `--allow-provider-calls` 时，RAG 评测只能读取冻结 Fixture，不访问外部 Provider。

生产 v2 评测需要独立、已核验的 MySQL/Redis 身份和专用锁；不得仅凭数据库名称认定隔离，不执行自动 reset、flush 或 DDL。评测报告不能把 Fixture 结果写成真实模型验收。

## 7. 停止、恢复与安全边界

```powershell
docker compose @fixture stop
docker compose @fixture start
# 只移除容器和网络，保留数据卷：
docker compose @fixture down
```

不要在未核对项目和备份的情况下使用 `down -v`。本地 HTTP Cookie 默认不启用 Secure，端口只绑定回环地址；不要改成公网监听。模拟订单不包含支付、配送、退款、退货或换货。

历史迁移 `000001`～`000010` 不得原位修改；V2 新迁移和迁移哈希检查按 Spec 和迁移恢复文档执行。
