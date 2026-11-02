# 项目启动说明

本项目的运行形态是 `bookstore-service` + `agent-api` + `agent-worker` + Nginx 静态 SPA。Node.js 只参与 Vite 构建和浏览器测试。下面的 Fixture 流程不会访问外部模型；真实模型流程必须在单独授权后执行。

## 1. 准备

在项目根目录打开 PowerShell 7，确认 Docker Desktop 已启动：

```powershell
Set-Location -LiteralPath 'D:\新建文件夹\书店微服务系统'
if (-not (Test-Path -LiteralPath '.env')) { .\deploy\init-local.ps1 }
```

`.env` 已存在时初始化脚本拒绝覆盖。不要输出完整 Compose 配置、`.env`、JWT 私钥、邮件令牌或数据库导出。

## 2. Fixture 本地运行

Fixture 只覆盖确定性协议和真实本地基础设施，不证明真实模型质量。空卷必须使用两阶段启动器，不能直接 `docker compose ... up -d --build`：

```powershell
.\deploy\start_v2_fixture.ps1 -Project bookstore-stage11
```

启动器会校验测试项目名，设置本地 Fixture 网关，先启动依赖/Go/model-fixture，按 ISBN 追加导入100本演示书，显式建立全量 RAG Manifest，再启动 Agent API、Worker 和 Nginx。它不删除卷、不清空表、不打印密钥；重复运行不会覆盖已有 ISBN 的价格、库存、状态或关系。

如果 `.env` 是 V2 之前创建的，先按 `deploy/init-local.ps1` 输出补齐 `MYSQL_WORKER_PASSWORD`；不要把密码写入命令行或提交到 Git。

Fixture 使用专用固定测试网段，同一时刻只保留一个 Fixture 项目；若已有另一个 Fixture 项目占用该网段，启动器会先报错，不会停止或删除它。

默认入口为 <http://127.0.0.1:8088>，验证邮件为 <http://127.0.0.1:8025>。`migrate` 和 `bootstrap` 是一次性初始化任务；RAG 索引按 Spec §11.7 显式执行，不由应用启动钩子调用。

运行 V2 Fixture smoke：

```powershell
.\agent\.venv\Scripts\python.exe deploy/e2e_smoke.py --project bookstore-stage11
```

它覆盖注册验证登录、Catalog 查询、Guest/Auth Chat、确认卡、订单、明确偏好、补货监听、Worker 重启、通知、Trace、Replay/Evals 404 和 SPA 深链接。测试账号、密码和测试数据只在进程/测试卷中使用，不会删除已有用户。

## 3. 真实模型（需单独授权）

先阅读 [模型接口配置](model-api-configuration.md)，在本机 `.env` 配置真实 Chat/Embedding 网关、模型和 HTTPS 地址。不要将 key 发到聊天，也不要因为 `.env` 存在就默认执行收费调用。

```powershell
$live = @('-p', 'bookstore-live-local', '-f', 'compose.yaml')
docker compose @live config --quiet
if ($LASTEXITCODE -ne 0) { throw '真实 Compose 配置检查失败' }
docker compose @live up -d --build # 仅适用于已有兼容 Manifest 的环境；空卷先按部署说明完成索引
```

普通部署不会自动调用 Provider 建索引；需要真实 Embedding 时，按 Spec 的两阶段流程显式运行索引命令，并记录模型、集合、版本、调用次数和费用状态。

## 4. 可选观测 Profile

默认不启动 Prometheus、Grafana、Tempo 和 OTel Collector。需要技术观测时，在同一测试项目显式启用：

```powershell
$observed = @('--profile', 'v2-app', '--profile', 'observability', '-p', 'bookstore-stage11', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml')
docker compose @observed up -d
.\agent\.venv\Scripts\python.exe deploy/verify_telemetry.py --project bookstore-stage11
```

观测检查只验证技术 Trace、指标、Grafana 数据源和敏感字段投影，不宣称业务推荐质量。完成后可停止项目但保留卷：

```powershell
docker compose @observed stop
docker compose @observed start
```

## 5. 分层检查

```powershell
$env:GOCACHE = "$PWD/.workcache/go-build"
$env:GOTMPDIR = "$PWD/.workcache/go-tmp"
go test ./...
go vet ./...
.\agent\.venv\Scripts\python.exe -m pytest agent/tests -q -p no:cacheprovider
npm run typecheck --prefix web
npm run build --prefix web
```

仅静态/单元检查不会产生 Provider 费用；真实页面、真实模型、邮件或数据库恢复验收需单独标注运行身份和证据状态。

## 6. 停止与清理

```powershell
$compose = @('--profile', 'v2-app', '-p', 'bookstore-stage11', '-f', 'compose.yaml', '-f', 'compose.e2e.yaml')
docker compose @compose stop
# 只移除容器和网络、保留数据卷：
docker compose @compose down
```

不要用未核对项目名的 `down -v`。切换 Fixture 和真实项目时，先停止当前项目，确认端口和卷归属，再启动另一个项目。
