# 条件推荐故障修复与验收（2026-11-03）

## 原因与修复

原失败 Run `3d575af7-6150-48b9-8844-f316f79a32a5` 的 VERIFICATION 安全投影包含两条 EVIDENCE_MISMATCH：推荐 book_id 1/9 只引用 Catalog，没有同书知识证据，因此进入 REPAIR。运行容器中确认 `app.api.chat` 未定义 invoke_model；修复分支先创建步骤再调用未导入函数，必然触发 NameError。原始日志已被投影为通用 RUNTIME_LOG，未取得该 Run 的原始异常栈；根因依据是运行模块与对应分支代码，不声称恢复了原始栈。

- 导入实际模型调用函数，保持原有校验和模型配置。
- REPAIR 模型调用、解析、动作检查失败或被取消时，先写 FAILED 步骤及安全错误信息再抛出，成功仍写 COMPLETED。
- NameError/AttributeError/TypeError 映射 INTERNAL_ERROR，不再误报依赖不可用；不向客户端暴露异常原文。
- 历史失败轨迹原样保留，没有改写成成功。

## 验收证据

真实 Edge、已验证测试账号，DeepSeek `deepseek-v4-flash`，Profile `deepseek-flash-v1`，Prompt `v4.2-single-agent-rag`。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 政策问答前置 | PASS | Run `08e2c538-5574-4a5b-8c9d-0693d48f7cf6`，页面显示来源 |
| 同会话随后发送原推荐问题 | PASS | Run `30e6b1b3-c65c-4c20-a3ac-016d94b3f31c`，返回两本，COMPLETED/COMPLETE，8步骤全部COMPLETED |
| 新会话直接发送原推荐问题 | PASS | Run `46a0cb19-78c0-4fb2-afdd-b2d06e32ddf3`，返回两本，COMPLETED/COMPLETE，8步骤全部COMPLETED |
| 当前商品事实复核 | PASS | GET /api/books/1 与 /api/books/28：作者含林甲、zh-CN、分布式系统、ACTIVE，分别3000/4999分、库存5/20，与页面一致 |
| REPAIR成功、非法JSON、内部异常、取消 | PASS（确定性单测） | 新增4例直接覆盖生产ChatService修复回调，不是浏览器或真实模型质量证据 |
| Python回归 | 206 passed，1 skipped | 缺 GUEST_TEST_REDIS_URL 的 Redis 集成测试跳过，不计通过 |

两次自然推荐均取得知识证据后直接通过校验，没有触发 REPAIR；因此真实模型 REPAIR 分支本身未取得本轮浏览器覆盖，分支异常保障由上述针对性单测提供。没有将错误快速返回计入成功性能；不报告浏览器精确端到端延迟。

## 部署

先临时更新原容器进行同会话验证，随后正常 Docker 构建并重建 Agent API；新会话测试在正式镜像完成。两者chat.py SHA-256一致：`143b445bea510d8da8ac156b54ffa4a05ddf1711c8840c4fce67a761e05ab673`。

正式镜像：`sha256:daf197a99c190999ea5e2ed157646efec548522ef4d560f66ec6de6291708f87`。Nginx已重载。Compose缺失的Worker密码从现有Worker容器复用，仅置于部署进程环境；未输出密钥、未覆盖.env、未重置数据。现有.env缺失该变量的问题仍需在未来完整启动前补齐，不属于本次推荐修复。

本地浏览器截图：`.workcache/repair-20261103/recommendation.png`；新会话Trace页面证据：`.workcache/repair-20261103/trace-direct.txt`。运行原始产物不上传公开仓库。
