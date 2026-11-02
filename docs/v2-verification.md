# V2当前验证快照

日期：2026-09-25

本文件只记录执行结果，不定义需求。规范、接口和验收门槛只以[单Agent RAG开发Spec](bookstore-single-agent-rag-development-spec.md)为准。

状态含义：`PASS`表示本次取得可重复证据；`FAIL`表示已执行但不符合；`BLOCKED`表示存在外部前置条件；`NOT_RUN`表示尚未执行，不能推断通过。

## 已取得证据

| 范围 | 状态 | 证据 |
|---|---|---|
| 文档一致性 | PASS | 当前Spec/DQ/验证快照的链接和代码围栏有效；`CONSISTENCY_CHECK=PASS`、`unresolved_DQ_V2=0`、`ambiguous_contracts=0` |
| Go回归 | PASS | `go test ./...`与`go vet ./...`通过 |
| Python回归 | PASS | 最新完整回归202 passed；1项Redis专用集成测试因未设置`GUEST_TEST_REDIS_URL`跳过，真实Redis边界已由Compose E2E覆盖 |
| Web静态检查 | PASS | TypeScript检查和Vite生产构建通过 |
| Web页面回归 | PASS | 独立3017端口执行Playwright 27项通过；新增单一Session请求与V2包装偏好值展示回归 |
| Web生产依赖审计 | PASS | `npm audit --omit=dev --audit-level=high`为0个漏洞 |
| MySQL迁移 | PASS | 空库、V1升级、PUBLISHED重放、Down→Up、dirty版本拒绝和迁移哈希保护通过真实MySQL测试 |
| V2 Compose Fixture | PASS | 服务图、最小数据库权限、邮件验证登录、Cookie、Catalog、游客/登录咨询、显式偏好、订单预览、Agent重启确认、幂等扣库存、Worker重启补货通知、Trace只读、Replay/Evals 404和SPA深链全部通过 |
| Qdrant Fixture索引 | PASS | 真实Qdrant中完成19个POLICY Chunk、882个BOOK_GUIDE Chunk的全量Point审计和双Alias切换；Fixture不代表真实Embedding质量 |
| RAG tuning Fixture | PASS | 24例，Recall@5/MRR/Section Recall均为1.0，误召回率0，硬过滤违反0，`provider_calls=0`、`answer_quality_status=NOT_RUN` |
| RAG holdout Fixture | PASS | 56例，Recall@5/MRR/Section Recall均为1.0，误召回率0，硬过滤违反0，`provider_calls=0`、`answer_quality_status=NOT_RUN` |
| 部分RAG构建与回滚 | PASS | 隔离故障注入覆盖POLICY/BOOK_GUIDE部分构建只切目标Alias、继承未更新语料Manifest，以及Alias切换失败不覆盖旧Manifest；使用内存Store，不宣称真实Qdrant故障演练 |
| 遗留EPISODE向量删除与重试 | PASS | fake HTTP/DB契约测试覆盖delete-only请求、200/404幂等、成功/暂时失败/达到重试上限三种终态；不宣称真实MySQL/Qdrant联合演练 |
| 真实Qwen Embedding tuning | PASS | 24例真实Provider评测选择256/32；POLICY阈值0.57149327、BOOK_GUIDE阈值0.32211316；Recall@5、Section Recall均1.0，MRR 0.9565，FPR 0，硬过滤违反0；210次批次调用、2775个文本输入，最终回答质量NOT_RUN |
| 真实Qwen版本化索引 | PASS | 使用独立验收Alias/Manifest完成1024维索引，19个POLICY Chunk、882个BOOK_GUIDE Chunk；未覆盖Fixture在线Alias |
| 真实Qwen holdout | FAIL | 56例：Recall@5 0.9556、MRR 0.9444和硬过滤通过；FPR 0.0909高于5%，原口径Section Recall 0.8732低于90%。失败报告保留，禁止用holdout反调阈值或表述为RAG验收通过 |
| 真实Qwen未见改写holdout v2.1/v2.2 | FAIL | v2.1首次运行Recall 0.9783、MRR 0.9457、Section Recall 0.9130、FPR 0.10；确认唯一误报是已有否定政策却被标成弃答。v2.2只修正该权威标签，首次运行Recall 0.9787、MRR 0.9362、FPR 0、硬过滤0，但Section Recall 0.8936低于0.90。两版报告均在Fixture runtime卷中按create-only保留，禁止继续用holdout调阈值或改标签追绿 |
| 真实Qwen RAG v3 tuning与隔离索引 | PASS | 24例经纠正标签的tuning选择256/32；POLICY阈值0.5432527、BOOK_GUIDE阈值0.5648611；隔离Alias包含3份政策/20个Chunk、98份书籍知识卡/882个Chunk，Manifest/source tree/Alias/Point计数一致 |
| 真实Qwen RAG v3首次holdout | PASS | 独立审核并冻结56例，首次Provider运行Recall@5 0.9796、MRR 0.9694、Section Recall 0.9796、FPR 0、硬过滤与禁入命中0；102次Provider调用、958个输入，`answer_quality_status=NOT_RUN`。唯一漏召回是“模型绕过确认门”未命中工具权限章节，但不影响≥90%门槛且运行时确认门由确定性代码验证 |
| 真实Chat只读咨询 | PASS | 本地Compose使用DeepSeek V4 Flash与真实Qwen索引；首页三书推荐、政策带来源、未知政策拒答、提示注入拒绝，以及服务雪崩/消息幂等/数据库索引三条语义咨询取得终态。语义咨询分别返回B26/B35/B33并披露不覆盖雪崩治理、B28、B39/B41；每次只有一个`message.completed` |
| RAG v3在线切换回归 | PASS | `agent-api`已切换至`bookstore_policy_real_v3/bookstore_book_real_v3`和`rag-index-manifest-real-v3.json`；真实DeepSeek下首页推荐、工具权限政策与知识库外会员积分三例均得到唯一`message.completed`。切换时发现模型首次生成非法工具参数；严格Schema未放宽，Harness新增一次参数修复机会并有单元回归 |
| 最新Fixture整栈复验 | PASS | 最新代码在保留命名卷的同一隔离项目中切换为`model-fixture`后完整通过：最小权限、游客、注册验证登录、Catalog、登录咨询、偏好写入→删除→规范化重建、订单预览/Agent重启/幂等确认、MySQL Outbox/Worker重启/单通知、Trace、Replay/Evals 404和SPA深链。随后恢复真实DeepSeek/Qwen环境并确认服务健康 |
| 旧运行层清理 | PASS | 生产路径不存在Supervisor、DAG Runner、LangGraph、Redis Streams Publisher/Consumer或业务Replay执行定义；负向测试和历史兼容字段不算当前能力 |

Fixture冒烟命令：

```powershell
$env:MYSQL_WORKER_PASSWORD = '仅用于隔离Fixture的测试值'
.\agent\.venv\Scripts\python.exe .\deploy\e2e_smoke.py `
  --project bookstore-v2-fixture `
  --base-url http://127.0.0.1:8088
```

## 本轮整栈校验发现并修复的问题

1. Qdrant滚动读取返回带连字符的UUID，审计端按无连字符Point ID比较导致误报；现统一规范化后再核对Payload身份。
2. Alias读取误用了`GET /collections/aliases`；现使用全局`GET /aliases`，更新仍使用`POST /collections/aliases`。
3. Fixture声明了不存在的模型Profile和模型名；现绑定冻结的`deterministic-fake-v2`。
4. 严格工具Schema改变后版本哈希未同步；现已冻结新的`live-v2`哈希并增加一致性测试。
5. Go部署YAML没有把`APP_BASE_URL/AGENT_API_URL`映射到配置，导致合法`127.0.0.1` Origin按默认`localhost`被拒；现显式绑定环境变量，不放宽Origin检查。
6. 最终校验器未识别`get_book`返回的直接`BookDetail`形状；现与Spec第9.3.1节统一并有回归测试。
7. 重启恢复用例只等待Go会话接口，未等待Agent API健康；现按Agent自身`/healthz`判定恢复。
8. DeepSeek兼容端点拒绝没有原生`tool_calls`前序的`role=tool`消息；Harness改为注入受控`LOCAL_TOOL_RESULT_JSON`系统消息，不启用Provider原生Tool Calling。
9. 政策Claim原先没有严格来源字段，导致模型回答在最终校验被拒；现由严格Schema要求`source_uri/section_path/version/evidence_ids`，政策真实页面返回文档、版本、章节、路径和Evidence ID。
10. 自然语言工程主题直接送入Catalog会得到空结果并反复检索；现按确定性意图收窄工具集，使用审核词典提供Catalog query hint，且BOOK_GUIDE仍负责证明或否定适配性。
11. `message.completed`曾直接携带Pydantic Recommendation对象，标准JSON序列化在全部delta发送后断流；现终态前显式`model_dump(mode="json")`并有回归测试。
12. Prompt停止规则不能覆盖同名历史版本，否则Trace启动校验会拒绝服务；现保留v4/v4.1并新增v4.2，所有版本按SHA-256归档。
13. 原holdout将JWT与自动退款等“知识库存在明确否定规则”的问题误标为必须弃答，并有B54～B57书号与主题错配。v2.1/v2.2保留为历史失败；v3重新逐条以书目/政策原文审核标签，经真实tuning、隔离索引和首次holdout达到门槛，未按holdout结果反调参数。
14. 当前Harness用`LOCAL_TOOL_RESULT_JSON`系统消息传递本地工具结果，但确定性模型Fixture仍只读取旧`role=tool`，导致重复调用直至预算耗尽。Fixture现统一解析当前Envelope并保留历史兼容读取；新增协议测试，完整Compose冒烟恢复通过。

## 运行环境与完整浏览器证据

| 范围 | 状态 | 继续验收所需动作 |
|---|---|---|
| 真实Chat完整浏览器用例 | PASS | 专用已验证账号通过首页三书、多轮比较、订单预览及拒绝、Trace、显式语言偏好、B11库存监听确认/创建、任务页ACTIVE与未触发通知空状态。监听Run先以`get_book`取得`book:11:v1` Evidence，卡片显示库存0、阈值2、24小时；未批准订单、未改B11库存。历史只读政策/无答案/提示注入/语义查询证据继续有效 |
| 当前本机真实Provider Compose直接启动 | PASS | 使用进程级随机`MYSQL_WORKER_PASSWORD`与验收覆盖文件启动；未写入`.env`、日志或文档。Agent API、Go业务服务、MySQL、Redis、Qdrant和Nginx健康 |

## 当前结论

V2 Spec、确定性代码回归、Fixture整栈链路、真实RAG v3门禁和要求的真实Provider认证浏览器旅程已取得通过证据。项目可表述为“本地可容器化验证的单Agent RAG工程MVP”，仍不能表述为已生产上线；生产流量、SLA、灾备、真实支付和运营数据均不在本次证据范围。
