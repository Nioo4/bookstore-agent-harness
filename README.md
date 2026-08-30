# Bookstore Agent Harness

基于现有 go-zero 书店微服务增量构建的智能客服系统。Go/MySQL 负责认证、商品、订单与库存事务；Python/LangGraph 负责 Agent 规划、工具调用、验证、记忆、任务恢复、Trace 与固定结果 Replay；Next.js 提供 Web 界面。

## 主要能力

- 邮箱注册、验证和 JWT 登录
- 多条件书籍检索、推荐、比较与连续咨询
- Supervisor 动态 DAG、专业 Agent、工具预算和 Evidence Verifier
- 用户显式确认后的模拟订单与原子扣库存
- 长期记忆、政策 RAG、补货监听和事件恢复
- Trace、固定工具结果 Replay 与 Agent 评测

## 本地启动

需要 Docker Desktop（Linux 容器）和 PowerShell 7。

```powershell
Copy-Item .env.example .env
./deploy/init-local.ps1
```

在 `.env` 中填写 `MODEL_API_KEY` 和 `EMBEDDING_API_KEY`，然后启动：

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
```

访问：

- Web：<http://127.0.0.1:8088/>
- Mailpit：<http://127.0.0.1:8025/>
- Grafana：<http://127.0.0.1:3001/>

停止服务：

```powershell
docker compose down
```

真实 `.env`、JWT 私钥和运行数据不会提交到仓库。项目不包含真实支付、退款和物流履约。
