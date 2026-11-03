# 精确ISBN事实查询修复与验收

日期：2026-11-03。原始失败记录保留于 performance-cost-tests/2026-11-03，不改写历史0/5结论。

## 根因与修改

deterministic_intent 原先将包含价格、书、ISBN的请求标为recommendation，模型由此生成推荐字段；validator要求推荐理由引用同书BOOK_GUIDE，最终保守回答又丢弃已查到的价格库存。业务失败不能按message.completed认定通过。

- 对明确ISBN价格/库存且不含推荐、适读性、比较、购买、政策、监听等语义的请求区分catalog_fact，只开放Catalog读取工具。
- 注入事实查询输出约束，要求空recommendations/policy_claims。
- 最终校验前，只从当前Run的Catalog结果中查找唯一匹配ISBN；金额、库存类型及币种通过检查才生成确定性价格库存摘要，删除无关语义文案和推荐卡。没有匹配证据不会合成事实。
- 推荐和混合问题不使用此投影，原知识证据校验代码未关闭。此阶段未扩展为任意事实问答系统。

## 实测

真实Edge页面、已验证账号、deepseek-v4-flash；每次重新加载首页产生独立会话，发送原问题。5/5页面显示“ISBN 9780000000019：价格 30.00 元，当前库存 5 本”。当前GET /api/books/1复核price=3000分、stock_count=5、CNY。

| 次数 | Run ID | 页面业务结果 |
| --- | --- | --- |
| 1 | 147acc41-7b2c-4e0a-98a0-29b78dab7c8e | PASS |
| 2 | da229f98-bd7e-481c-bef7-70bc258a793a | PASS |
| 3 | 24ab684f-88dc-4db2-b2af-aa904307f7d1 | PASS |
| 4 | f6dff3f7-6ecd-4130-b00e-a770a1e0b031 | PASS |
| 5 | fcbbb2d5-6e82-4584-8b57-2c7a07a5d11f | PASS |

五个Trace逐条核对：COMPLETED/COMPLETE；四步骤全部COMPLETED（模型→get_book→模型→VERIFICATION），0次REPAIR。每次2次聊天调用；Token分别4150、4201、4201、4201、4201。没有将控制工具观察耗时作为浏览器性能指标，本报告不作性能承诺。

真实推荐回归：“推荐两本林甲写的中文分布式书，有货，每本50元以内。”返回book_id 1/28，30元库存5、49.99元库存20，展示BOOK_GUIDE理由和推荐卡，未被事实查询路径接管。

代码测试：215 passed、1 skipped（缺GUEST_TEST_REDIS_URL，不计通过）。新增测试覆盖意图分离、混合请求保留推荐、无关推荐移除、错误模型价格被实时事实替换、无有效Catalog不可合成事实；同一带Catalog引用但无知识引用的推荐先验证被拒，确认没有整体关闭校验。

正式Agent镜像：sha256:c365abce3ae634d75cc1bbb773f82d4cb6a2b7821783bc5135273d8974f80cf8；已重建Agent API并重载Nginx，未修改密钥、库存、模型额度或重建向量库。截图和五条Trace快照留在本地.workcache/catalog-fact-20261103，不公开上传。

额外观察：Trace用户问题中ISBN仍显示[REDACTED]，这是已有脱敏展示问题，本次未修改；不影响聊天正文的ISBN、价格及库存。
