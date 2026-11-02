# 书店项目说明与 ChatGPT 接手指南

整理日期：2026-10-20。用途：将本文件直接上传给新 ChatGPT 会话，使其理解项目背景、当前架构、功能边界、实现入口和证据状态。相对路径均以仓库根目录为起点。

## 1. 首先理解项目和版本

仓库名称是“书店微服务系统”，本地路径为 `D:\新建文件夹\书店微服务系统`。当前产品是一个本地可容器化部署的单 Agent RAG 书店客服工程 MVP。用户可以用自然语言找书、比较适读性、查询政策、准备模拟订单并创建补货监听。模型负责理解、选择受限工具和解释，交易和权限由确定性代码负责。

必须区分三个概念：

| 名称 | 含义 | 使用方式 |
| --- | --- | --- |
| V1 历史架构 | 多 Agent、Supervisor、动态 DAG、LangGraph、独立 RPC 服务、Next.js、Redis Streams | 解释历史演变，不用于描述当前运行架构 |
| V2 当前实现形态 | 单 Agent、合并 Go 业务服务、React/Vite 静态 SPA、MySQL Outbox Worker | 当前源码、README 和部署形态 |
| Spec 3.0.0 | 2026-09-25 冻结的后续开发规范 | 当前唯一产品与工程规范；版本号不表示另有一套已上线的 V3 系统 |

**当前规范入口是 `docs/bookstore-single-agent-rag-development-spec.md`。** 它明确将 `bookstore-single-agent-rag-refactor-spec.md` 降为重构历史。README 和项目面试说明仍有指向旧 Spec 的文字，阅读时应按开发冻结 Spec 第 0.1 节处理。本说明是交接材料，不能覆盖规范或证明未执行的验收。

2026-10-20 本次核对了文档、依赖声明、Compose 和主要代码入口，没有启动服务、查询在线数据库或重新运行验收。后文带数字的验收成绩均属于 2026-09-25 的记录，不能称为今天重新测得。

## 2. 用户能力和产品边界

| 用户操作 | 系统行为 |
| --- | --- |
| 注册、邮箱验证、登录、登出 | 邮箱账号、验证邮件、HttpOnly Cookie 会话；导航显示昵称 |
| 按主题、作者、语言、预算和库存找书 | 查询当前 Catalog，保留显式硬条件 |
| 比较书籍、询问入门基础和适读性 | 将实时商品信息与 BOOK_GUIDE 知识证据组合回答 |
| 问系统政策 | 检索 POLICY 文档，附来源和版本；没有依据时明确无法确认 |
| 同页追问“第二本”“预算改成30元” | 登录会话保存短期条件和展示顺序，当前事实重新读取 |
| 购买 | 先生成订单预览，用户点击确认后才创建模拟订单和扣库存 |
| 补货监听 | 先核对对象和阈值，确认后创建任务，达到阈值产生站内通知 |
| 保存偏好 | 用户在 UI 显式保存结构化偏好，可查询和删除 |
| 查看执行过程 | 登录用户查看自己的脱敏 Trace 时间线、工具、Evidence 和用量 |

游客只支持单轮咨询，不保存持久 Trace 或长期偏好。未验证邮箱用户可登录并只读咨询，订单预览、确认和补货监听要求邮箱验证。

系统不提供真实支付、发货、物流、退款、退换货、自动购买、历史会话平台或公开生产 SLA。当前不提供在线业务 Replay、模型竞技场或 LLM Judge。公开 Evals/Replay 路由按当前契约返回 404。

## 3. 架构和职责

```text
浏览器 React/Vite SPA
  → Nginx 静态资源与同源 /api 代理
  → Go bookstore-service HTTP/BFF
      ├─ Auth / Catalog / Inventory / Order → MySQL、Redis
      └─ 允许列表内的 HTTP/SSE 代理 → Python agent-api
            ├─ 单 Agent Harness → Chat Provider
            ├─ Catalog / Order 工具 → Go 内部 gRPC
            ├─ RAG → Embedding Provider、Qdrant
            ├─ 短期会话 → Redis
            └─ 偏好、Trace → MySQL

Python agent-worker → MySQL Outbox 租约 → 补货任务和通知
                   → 到期与清理任务、遗留记忆向量删除
```

Go→Agent 聊天代理的当前实现说明为 HTTP/SSE；Agent→Go 业务工具使用内部 gRPC。开发冻结 Spec §2 总图将 Go→Agent 箭头标为 internal gRPC，与 README 的代理描述存在差异。接手时以实际代理入口核对传输协议，涉及契约修改再按 Spec 决策流程处理，不凭图示重写调用链。

| 组件 | 职责与数据边界 |
| --- | --- |
| Go 业务服务 | 单进程提供 HTTP/BFF、认证、目录、库存、订单及内部 gRPC；不调用模型 |
| Agent API | 单模型角色的受控循环、Context、RAG、工具适配、最终校验和 SSE |
| Agent Worker | 不调用模型；消费 MySQL Outbox，处理通知和清理 |
| MySQL | 用户、商品、库存、订单、任务、通知、偏好、Trace 和 Outbox 的权威存储 |
| Redis | 缓存、限流和短期上下文；不是交易事实库或库存事件总线 |
| Qdrant | POLICY/BOOK_GUIDE 派生索引，可以从原始资料重建；不提供权威实时库存 |

Compose 默认有 10 个服务定义，其中 `migrate`、`bootstrap` 是一次性任务，其余 8 个是常驻服务：MySQL、Redis、Qdrant、Mailpit、Go、Agent API、Agent Worker、Nginx。测试覆盖文件可增加 model-fixture。观测 Profile 另含 Prometheus、Grafana、Tempo、OTel Collector。

Node.js 用于前端构建和测试，不是当前生产运行服务。`rpc` 目录仍然存在，不代表每个 RPC 模块都有独立运行容器。仓库中遗留文件名也不能直接视为在线能力。

## 4. 技术栈和代码导航

当前依赖文件显示 Go 1.26.1、go-zero 1.10.1，Python 3.12、FastAPI、Pydantic 2、grpcio/httpx，以及 React 19、Vite 7、TypeScript、React Router。数据库组件为 MySQL 8.0、Redis 7、Qdrant 1.15。具体补丁版本以依赖锁文件和镜像配置为准。

| 路径 | 阅读目的 |
| --- | --- |
| `cmd/bookstore-service/` | 当前 Go 运行入口 |
| `api/` | HTTP 路由、BFF、认证入口、业务调用；`api/bookstore.api` 为接口字段入口 |
| `rpc/model/` | 业务模型、目录查询、库存与持久化适配 |
| `rpc/check/check.proto`、`rpc/order/bookstore_order.proto` | 跨语言 Catalog/Order 契约 |
| `agent/app/main.py`、`agent/app/api/app.py` | Agent 启动与路由装配 |
| `agent/app/api/chat.py` | 单次聊天接线、SSE、会话、确认卡和 Trace |
| `agent/app/harness/single_agent.py`、`validator.py` | 模型循环、严格输出协议、预算与最终校验 |
| `agent/app/tools/` | 工具 Schema、权限、风险、超时、重试及业务客户端 |
| `agent/app/api/conversation_context.py`、`conversations.py` | 当前条件、指代、短期状态与用户归属 |
| `agent/app/rag/` | 切片、索引、Qdrant、检索、Evidence 和评测 |
| `agent/app/rag_index.py`、`rag_evaluation.py` | RAG 命令入口 |
| `agent/app/memory/` | 显式偏好和遗留向量删除；不是自动情节记忆系统 |
| `agent/app/tasks/v2_worker.py`、`v2_repository.py` | MySQL Outbox 租约与任务处理 |
| `agent/app/trace/` | Trace、脱敏、版本身份与用量 |
| `agent/prompts/`、`agent/config/` | Prompt、工具、模型配置及版本清单 |
| `web/src/App.tsx`、`web/src/lib/`、`web/src/styles.css` | 当前 SPA、共享会话和界面样式 |
| `migrations/` | 版本迁移及哈希基线，含 `000011_single_agent_runtime` |
| `knowledge/policies/`、`knowledge/books/` | 政策和书籍知识卡源文档 |
| `deploy/`、`compose.yaml`、`compose.e2e.yaml` | 启动、构建、Nginx、Fixture 和整栈验收 |

旧说明中的 `web/app/navigation.tsx`、Supervisor 或 DAG Runner 不应被当作今天的修改入口。

## 5. 一次咨询的执行过程

1. 浏览器向 `/api/chat/conversations/:id/messages` 发请求，Go BFF 校验来源和 Cookie，再代理到 Agent。
2. 服务端确认用户和会话归属，读取短期上下文及相关显式偏好，解析本轮硬条件与指代。
3. 按确定性意图选择最小工具集，构造固定顺序 Context。
4. 模型输出严格 JSON：`CALL_TOOLS` 或 `FINAL`。Harness 校验字段、权限、预算和工具参数。
5. 工具经普通 Registry 执行，将安全结果和应用生成的 Evidence ID 加入 Context。
6. 最终校验推荐身份、价格、库存、硬条件和引用。需要时执行一次修复，仍不符合则安全失败或保守回答。
7. SSE 返回结果；每个请求必须恰好有一个终态：`message.completed` 或 `run.failed`。

当前规范预算是最多 6 次模型调用、12 次逻辑工具调用、3 个并行只读调用、120 秒请求时限；技术失败最多重试一次，最终修复一次。相同 Run 的相同只读调用可复用结果，PREPARE 不套用该去重规则。

模型输出的“工具调用”是 Harness 自行解析的 JSON，不是 Provider 原生 Tool Calling。当前工具返回通过受控 `LOCAL_TOOL_RESULT_JSON` 消息传入，不能发送没有原生 `tool_calls` 前序的 `role=tool`。

工具能力包括 `search_books`、`get_book`、`search_book_knowledge`、`search_policy`、用户范围订单读取及有条件开放的 `prepare_order`。`create_order` 和创建监听不在模型白名单中。

## 6. Context 和记忆

短期会话在 Redis 保存最近 10 个完整轮次，总正文不超过 8000 字符，闲置 24 小时失效。它保存有效条件、展示顺序和选择对象，用于理解“第二本”“其他要求不变”。

历史只提供定位信息。比较、购买或书籍知识检索前，必须在当前 Run 重新通过 `get_book` 取得事实；这次读取仍计入预算、Trace 和 Evidence。历史价格和库存不能拿来成交。

长期偏好由用户 UI 显式保存；当前明确要求优先于偏好，临时预算不自动转成长期记忆。V1 自动提取和 EPISODE 语义记忆不再作为当前能力。遗留 EPISODE 删除由 Worker 对指定旧集合做 delete-only 清理。

## 7. RAG 的证据链

POLICY 回答系统规则与能力边界，BOOK_GUIDE 描述演示书的适读人群、前置知识、主题和不覆盖内容。二者都不能替代 Catalog 的实时价格、库存和状态。

索引流程：校验 Markdown 元数据 → 按 ISBN 关联环境内 Catalog → 优先 H2 切片、长章节滑窗 → 生成内容哈希和稳定 Point ID → Embedding → 新物理 Collection → 全量审计 → 冒烟检索 → 原子切换 Alias → 更新 Manifest。

知识索引不在应用启动时自动调用收费 Embedding。Manifest 保存向量空间、源版本、集合与参数等身份，应用启动核对一致性。书籍知识检索要求已有当前 Run 的 Catalog 候选，再按 book_id 过滤，避免知识卡推荐下架或不符合硬条件的书。

当前规范使用 Qwen `qwen3.7-text-embedding`、1024 维；实际网关和模型以部署配置核对，Chat 和 Embedding 分开配置。Token 切片用 `cl100k_base` 工程估算，不等于 Qwen 内部计费 Token。

先用 tuning 选择切片和阈值，再冻结 holdout 做独立评估。不得根据 holdout 结果反复调参数追求通过。检索指标和最终回答质量必须分开报告。

## 8. 订单与补货监听

订单流程是“实时读取 → prepare_order → pending_action 和确认卡 → 用户按钮确认 → Go 单个 MySQL 事务”。确认事务重新检查身份、令牌、价格、库存和状态，再扣库存、创建订单、保存结果与 Outbox；失败全部回滚。

相同决定重放返回原结果；相反决定属于确认终态冲突，返回 `409 CONFIRMATION_ALREADY_DECIDED`；同一幂等键用于不同请求内容返回 `409 IDEMPOTENCY_CONFLICT`。模型口头说“确认”不能替代按钮/API。

补货监听要求唯一对象、目标数量和期限。当前库存低于目标数量才需要监听，并非只有零库存能监听。确认 API 再复核；已经满足时返回 `WATCH_NOT_NEEDED`。

库存变化与 Outbox 行同事务提交。Worker 用 MySQL 租约及 `FOR UPDATE SKIP LOCKED` 领取事件，结合消费记录、任务状态和通知唯一键，使重试收敛为一次通知效果。这里是“至少一次处理 + 业务幂等”，不能宣传成消息绝不重复。通知不会自动下单。

## 9. 身份和 Trace

密码用 Argon2id 加盐哈希，JWT 用 Ed25519，验证/确认令牌只持久保存摘要。浏览器身份来自 HttpOnly Cookie；写请求检查与 `APP_BASE_URL` 一致的 Origin。`localhost` 和 `127.0.0.1` 不是同一 Origin，入口必须与实际配置一致。

user_id 来自服务端认证，不能接受模型指定的用户身份。订单、偏好、任务、通知和 Trace 都按当前用户隔离。JWT、Cookie、密码、API Key、原始确认令牌、隐藏 Prompt 和思维链不得写进公开证据或交接文档。

Trace 是安全投影后的线性时间线，记录模型、工具、Evidence、耗时、用量和错误。Token/费用未知不能填 0；运行完成也不能直接等同于业务回答正确。原始聊天内容会发送给所配置的模型 Provider，应准确理解此数据边界。

## 10. 运行和验证入口

先进入仓库，在 PowerShell 7 和 Docker Desktop Linux 容器环境执行。首次配置参考 `deploy/init-local.ps1` 和 `.env.example`；已有 `.env` 不应覆盖。不要将完整 `.env` 或渲染后的含密钥 Compose 配置发给 ChatGPT。

无需真实 Provider 的隔离测试环境：

```powershell
Set-Location -LiteralPath 'D:\新建文件夹\书店微服务系统'
.\deploy\start_v2_fixture.ps1 -Project bookstore-stage11
.\agent\.venv\Scripts\python.exe deploy/e2e_smoke.py --project bookstore-stage11
```

启动器先启动依赖和 Go，按 ISBN 追加演示书并建立 Fixture RAG Manifest，再启动应用。Fixture 固定测试网段同一时刻只运行一个项目，先核对已有项目和端口占用。默认文档入口为 `http://127.0.0.1:8088`，验证邮件为 `http://127.0.0.1:8025`。

真实模型流程参考 `docs/code-usage-guide.md`、`docs/deployment.md` 和 `docs/model-api-configuration.md`。空卷需要显式索引的两阶段初始化，不能把已有 Manifest 环境的直接 `up` 命令用于所有环境。历史会话中的预算授权不代表新 ChatGPT 会话可自行提高额度或更换收费策略。

开发回归入口：

```powershell
$env:GOCACHE = "$PWD/.workcache/go-build"
$env:GOTMPDIR = "$PWD/.workcache/go-tmp"
go test ./...
go vet ./...
.\agent\.venv\Scripts\python.exe -m pytest agent/tests -q -p no:cacheprovider
npm run typecheck --prefix web
npm run build --prefix web
npm run test:e2e --prefix web
```

停止指定项目优先使用 `docker compose … stop`；`down` 删除容器/网络但保留命名卷，`down -v` 会删除卷，不能混用。重建上游后还要检查 Nginx 是否需要重载以刷新 upstream。

## 11. 数据与验收现状

演示数据集为 `bookstore-manual-v1`，包含 100 本演示书；书目和知识卡用于测试场景，不应包装成真实出版物全文。B11 为监听样本，B23 为缺失元数据样本，B25/B26/B27 为 Alice 中文书样本。价格、库存、ID 和在售状态可能随环境及测试改变，使用前实时查询，不能把历史快照写成永久事实。

2026-09-25 的 `docs/v2-verification.md` 记录：

| 验证层 | 已记录结果及边界 |
| --- | --- |
| 代码回归 | Python 202 passed、1 skipped；Go test/vet 通过；Web typecheck/build 和 27 项页面回归通过 |
| Fixture 整栈 | 注册验证、咨询、偏好、订单确认恢复/幂等、Worker 恢复/通知、Trace、已删除路由和 SPA 深链通过 |
| 真实 RAG v3 holdout | 56 例；Recall@5 0.9796、MRR 0.9694、Section Recall 0.9796、FPR 0；最终回答质量仍单列 |
| 真实模型浏览器 | 推荐、比较、预览并拒绝、Trace、显式偏好、监听创建及任务页状态等取得记录 |
| 生产部署 | 没有公开生产流量、SLA、灾备或真实交易运营证据 |

旧真实 RAG holdout 和改写 v2.1/v2.2 曾失败，报告保留；不能删去失败、把 Fixture 指标当真实模型质量或把检索指标当最终回答准确率。本次没有读取 runtime 卷中的原始评测产物，因此上述为已存在验证快照的摘要。

## 12. 历史教训与继续工作的方式

8 月咨询失败集中于运行镜像与源码不一致、目录参数和主题表达不匹配、后端缺少短期上下文、事实校正覆盖合法解释，以及测试身份和入口不一致。9 月重构又发现 Provider 消息格式不兼容、政策引用字段不完整、SSE 终态对象无法 JSON 序列化、Fixture 未同步当前消息协议等问题。应从具体失败所在层定位，不能笼统归因为模型能力不足。

登出悬停问题也说明：程序化直接点击通过，不代表用户移动鼠标的实际路径可用。当前 UI 应在 Vite 源码中检查；旧 Next.js 修复只作为历史经验。

用户希望主智能体负责判断与实现，按阶段维护 `docs/operation-log.md` 和 `docs/engineering-journal.md`，不为每个小改动重复长日志。操作日志记录本阶段做了什么、验证和剩余问题；工程日志记录关键判断、失败原因和修正依据。未经新的明确要求，不启动子智能体。

后续建议按以下顺序读资料：

1. 本说明，建立系统模型。
2. `docs/bookstore-single-agent-rag-development-spec.md`，确认当前需求和验收约束。
3. 当前源码和接口定义，区分规范要求与实际实现。
4. `docs/v2-verification.md`，定位历史证据及对应产物。
5. `docs/code-usage-guide.md`、`docs/deployment.md`，核对运行前置。
6. 历史 Spec、DQ、复盘和日志，只在需要解释演变时查阅。

需要工程改动时，先检查当前分支、未提交变更、实际镜像、配置身份和相关测试。普通局部实现可以直接推进；规范存在影响接口、交易、权限或迁移的互斥要求时，按当前 Spec 的 DQ-V3 流程处理。不要以修改测试预期来掩盖业务不符合，也不要重做已移除的 V1 架构。

## 13. 上传本文件时可一起发送的提示词

> 请把附件作为我的书店项目背景资料。先说明当前架构、业务事实来源、Agent 权限边界、订单确认流程和 RAG 证据链。请区分 V1 历史架构、V2 当前代码形态及 Spec 3.0.0 的规范版本。回答时区分源码事实、规范要求、历史验收记录和你的建议。你无法访问我本地仓库时，不要声称已经检查或运行文件；需要精确代码判断时，请指出应补充的具体文件。后续根据我的问题进行讲解、审查或实现，不自行扩展项目范围。

只上传本文件足以建立项目背景；精确调试还需补充相关源码、当前规范和脱敏错误。相对链接是仓库内导航，不代表普通 ChatGPT 能自动读取本机文件。
