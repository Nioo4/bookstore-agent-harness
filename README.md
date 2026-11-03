# 书店微服务系统

一个支持自然语言找书的单 Agent RAG 书店应用，提供条件推荐、书籍比较、带来源的政策问答、模拟订单、补货监听、个人偏好和执行轨迹。

前端使用 React/Vite，Go 负责账号、商品、库存和订单，Python 负责 Agent、知识检索和后台任务，配合 MySQL、Redis、Qdrant。订单需要用户确认，补货只发送站内通知，不自动下单。本项目用于本地演示，不提供真实支付、配送或退款。

## 准备环境

安装 Git、Docker Desktop（Linux 容器模式）、PowerShell 7 和 Python 3.12。以下命令在 PowerShell 中执行，建议使用英文目录，避免部分 Docker 环境的路径兼容问题。

```powershell
git clone https://github.com/Nioo4/bookstore-agent-harness.git
cd bookstore-agent-harness
.\deploy\init-local.ps1
```

最后一条命令生成本地 `.env`；已有配置时跳过，不要覆盖。下面两种启动方式任选其一，不要同时占用默认端口。

## 快速体验：模拟模型

```powershell
.\deploy\start_v2_fixture.ps1 -Project bookstore-stage11
```

启动器构建服务、初始化数据库、导入100本合成演示书并建立测试知识索引。首次启动需要下载依赖，请等待完成。

- 书店：<http://127.0.0.1:8088>
- 验证邮件：<http://127.0.0.1:8025>

此模式无需模型密钥，不调用付费服务，使用固定模拟响应，适合体验页面和业务流程，不代表真实自然语言推荐质量。同一时刻只运行一个模拟环境。

## 使用真实模型

此模式可能产生聊天和 Embedding 费用。编辑根目录 `.env`，分别填写：

```dotenv
APP_BASE_URL=http://localhost:8088
MODEL_BASE_URL=你的聊天接口地址
MODEL_API_KEY=你的聊天密钥
MODEL_ADAPTER=deepseek-chat-json-v1
AGENT_MODEL=你的兼容DeepSeek模型名称
EMBEDDING_BASE_URL=你的向量接口地址
EMBEDDING_API_KEY=你的向量密钥
EMBEDDING_MODEL=你的Qwen向量模型名称
EMBEDDING_DIMENSIONS=1024
```

其余设置保留生成值。接口和模型需兼容当前适配器，不要提交密钥。访问地址必须与 `APP_BASE_URL` 一致，不要混用 localhost 和 127.0.0.1。

### 首次启动

先初始化业务服务并导入演示书：

```powershell
$live = @('--profile', 'v2-app', '-p', 'bookstore', '-f', 'compose.yaml')
docker compose @live config --quiet
docker compose @live up -d --build mysql redis qdrant mailpit migrate bootstrap bookstore-service
python deploy/seed_manual_dataset.py --project bookstore --apply
docker compose @live build agent-api
```

空数据卷需要显式建立真实知识索引。以下命令会调用 Embedding 服务；调优结果必须满足检查要求。如果任一步失败，先解决错误，不要继续：

```powershell
docker compose @live run --rm --no-deps agent-api python -m app.rag_evaluation run --split tuning --allow-provider-calls --output runtime/initial-tuning.json
docker compose @live run --rm --no-deps agent-api python -m app.rag_index build --corpus all --tuning-report runtime/initial-tuning.json --allow-provider-calls
docker compose @live up -d agent-api agent-worker nginx
docker compose @live ps -a
```

报告不覆盖同名文件，重试时应更换报告文件名，并同步修改索引命令参数。不要用模拟索引替代真实向量索引。

访问 <http://localhost:8088>，在 <http://localhost:8025> 查看验证邮件。

### 后续启动与停止

已有兼容索引且未更换向量模型时：

```powershell
$live = @('--profile', 'v2-app', '-p', 'bookstore', '-f', 'compose.yaml')
docker compose @live up -d --build
docker compose @live ps -a
```

停止真实模型环境：

```powershell
docker compose @live stop
```

停止模拟环境：

```powershell
docker compose --profile v2-app -p bookstore-stage11 -f compose.yaml -f compose.e2e.yaml stop
```

停止命令保留数据。不要使用 `down -v`，它会删除数据卷。

## 如何使用

1. 在书店注册账号，进入 Mailpit 打开验证邮件，完成邮箱验证后登录。Mailpit 是本地收件箱，不会向真实邮箱投递邮件。
2. 在咨询页输入找书条件，例如“推荐两本林甲写的中文分布式书，有货，每本50元以内”，或询问“订单确认规则是什么？请注明来源”。
3. 在同一会话继续追问，例如“第二本适合初学者吗？”或“预算改为30元，其他条件不变”。
4. 要购买时请求订单预览，核对商品、数量和金额后点击确认，或拒绝取消。模拟订单确认会扣减演示库存。
5. 对库存不足的图书请求补货监听，明确图书、目标数量和期限，核对后确认创建。在任务和通知页面查看状态；库存达到目标阈值才会产生通知。
6. 在偏好页面保存或删除偏好，在执行轨迹页面查看自己的工具调用、证据与错误。

游客支持单轮咨询；多轮会话、个人偏好和轨迹需要登录，订单及监听还需要验证邮箱。书目是合成演示数据，库存随测试变化。以上问句是使用示例，不代表当前版本每个场景均已验收通过。

## 常见问题

- 端口占用：先停止占用8088或8025的旧书店环境，不要同时运行两种模式。
- 初始化状态：`migrate` 和 `bootstrap` 是一次性任务，正常退出不代表故障。
- 服务报错：真实模型环境执行 `docker compose @live logs --tail 100 agent-api bookstore-service` 查看日志，分享前移除敏感信息。
- 注册或写操作失败：检查浏览器地址与配置是否一致、邮箱是否验证。
- 咨询返回错误：在执行轨迹中记录 Run ID 和错误码。服务启动成功不等于所有推荐场景通过测试。
