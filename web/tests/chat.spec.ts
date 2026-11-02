import { expect, test } from "@playwright/test";

const completedStream = [
  'event: run.started\ndata: {"run_id":"run-1","trace_persisted":false}\n\n',
  'event: agent.started\ndata: {"status":"RUNNING"}\n\n',
  'event: tool.completed\ndata: {"status":"COMPLETED"}\n\n',
  'event: verification.completed\ndata: {"issue_count":0,"repaired":false}\n\n',
  'event: message.delta\ndata: {"content":"这本书符合要求。"}\n\n',
  'event: message.completed\ndata: {"content":"这本书符合要求。","recommendations":[{"book_id":7,"price":8800,"currency":"CNY","available_stock":5,"reason":"满足预算与库存要求"}]}\n\n',
].join("");

test("streams a verified recommendation into the chat", async ({ page }) => {
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: completedStream.replaceAll("\n", "\r\n") }),
  );
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /从一本好书开始/ })).toBeVisible();
  await expect(page.getByText(/本项目不保存对话、应用 Trace 或记忆/)).toBeVisible();
  await expect(page.getByText(/仍会发送给当前配置的模型 Provider/)).toBeVisible();
  await page.getByLabel("输入咨询内容").fill("推荐一本 100 元以内的书");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("这本书符合要求。")).toBeVisible();
  await expect(page.getByText("88.00 CNY")).toBeVisible();
  await expect(page.getByText("事实与约束核验完成")).toBeVisible();
});

test("makes the homepage distributed-systems budget explicitly per book", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("button", { name: "推荐三本每本不超过100元且有货的分布式系统入门书" }).click();

  await expect(page.getByLabel("输入咨询内容")).toHaveValue("推荐三本每本不超过100元且有货的分布式系统入门书");
});

test("renders message deltas before the stream completes", async ({ page }) => {
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (String(input).includes("/api/chat/conversations/")) {
        const encoder = new TextEncoder();
        const body = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('event: message.delta\r'));
            window.setTimeout(() => controller.enqueue(encoder.encode('\ndata: {"content":"正在形成回答"}\r\n\r\n')), 50);
            window.setTimeout(() => {
              controller.enqueue(encoder.encode('event: message.completed\rdata: {"content":"回答完成","recommendations":[]}\r\r'));
              controller.close();
            }, 800);
          },
        });
        return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
      }
      return originalFetch(input, init);
    };
  });
  await page.goto("/");
  await page.getByLabel("输入咨询内容").fill("查询");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("正在形成回答")).toBeVisible();
  await expect(page.getByText("回答完成")).toBeVisible();
});

test("shows a readable retry message when the catalog RPC is unavailable", async ({ page }) => {
  const stream = 'event: run.failed\ndata: {"code":"BUSINESS_RPC_ERROR","retryable":true}\n\n';
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: stream }),
  );
  await page.goto("/");
  await page.getByLabel("输入咨询内容").fill("查询 Alice 中文书的价格和库存");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("本次查询服务暂时不可用，请稍后重试（错误代码：BUSINESS_RPC_ERROR）。")).toBeVisible();
});

test("developer mode shows structured metadata without reasoning", async ({ page }) => {
  const stream = [
    'event: run.started\ndata: {"run_id":"run-1","trace_id":"trace-1","trace_persisted":true}\n\n',
    'event: agent.started\ndata: {"agent":"single_agent","round":1,"status":"RUNNING"}\n\n',
    'event: message.completed\ndata: {"content":"完成","recommendations":[]}\n\n',
  ].join("");
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: stream }),
  );
  await page.goto("/");
  await page.getByLabel("开发者模式").check();
  await page.getByLabel("输入咨询内容").fill("查询");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.locator(".dev-data")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("chain of thought");
});

test("approves a confirmation through JSON without exposing the token", async ({ page }) => {
  const stream = [
    'event: confirmation.required\ndata: {"action_type":"CREATE_ORDER","confirmation_token":"secret-token","items":[{"title":"Agent Systems","quantity":1,"line_total":8800}],"total_amount":8800,"currency":"CNY","expires_at":"2026-09-25T12:00:00.000Z"}\n\n',
    'event: message.completed\ndata: {"content":"请确认订单预览。","recommendations":[]}\n\n',
  ].join("");
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: stream }),
  );
  await page.route("**/api/chat/conversations/*/confirmations", async (route) => {
    const request = route.request();
    expect(request.url()).not.toContain("secret-token");
    expect(request.postDataJSON()).toEqual({ confirmation_token: "secret-token", decision: "APPROVE" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      confirmation_id: "action-1", decision: "APPROVE", status: "COMPLETED",
      order: { order_id: 91, status: "PLACED", total_amount: 8800, currency: "CNY" },
      replayed: false, trace_id: "trace-1",
    }) });
  });
  await page.goto("/");
  await page.getByLabel("输入咨询内容").fill("准备购买");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByRole("heading", { name: "订单预览" })).toBeVisible();
  await page.getByRole("button", { name: "确认并创建模拟订单" }).click();
  await expect(page.getByRole("heading", { name: "模拟订单已创建" })).toBeVisible();
  await expect(page.getByText(/订单 #91/)).toBeVisible();
  await expect(page.locator("body")).not.toContainText("secret-token");
});

test("rejects a confirmation without creating an order", async ({ page }) => {
  const stream = [
    'event: confirmation.required\ndata: {"action_type":"CREATE_ORDER","confirmation_token":"secret-token","items":[{"title":"Agent Systems","quantity":1,"line_total":8800}],"total_amount":8800,"currency":"CNY","expires_at":"2026-09-25T12:00:00.000Z"}\n\n',
    'event: message.completed\ndata: {"content":"请确认订单预览。","recommendations":[]}\n\n',
  ].join("");
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: stream }),
  );
  await page.route("**/api/chat/conversations/*/confirmations", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ confirmation_token: "secret-token", decision: "REJECT" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      confirmation_id: "action-1", decision: "REJECT", status: "CANCELLED", order: null,
      replayed: false, trace_id: "trace-1",
    }) });
  });
  await page.goto("/");
  await page.getByLabel("输入咨询内容").fill("准备购买");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "拒绝" }).click();

  await expect(page.getByRole("heading", { name: "已拒绝订单预览" })).toBeVisible();
  await expect(page.getByText("未创建订单，也未扣减库存。")).toBeVisible();
});

test("creates an inventory watch only after the visible confirmation card", async ({ page }) => {
  const stream = [
    'event: watch_confirmation.required\ndata: {"action_type":"CREATE_INVENTORY_WATCH","conversation_id":"conversation-1","book":{"book_id":7,"title":"Agent Systems","available_stock":0},"desired_quantity":1,"expires_at":"2026-09-25T12:00:00.000Z","submit_path":"/api/tasks/inventory-watches"}\n\n',
    'event: message.completed\ndata: {"content":"当前无货，可以创建补货监听。","recommendations":[]}\n\n',
  ].join("");
  await page.route("**/api/chat/conversations/*/messages", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: stream }),
  );
  await page.route("**/api/tasks/inventory-watches", async (route) => {
    const request = route.request();
    expect(request.headers()["idempotency-key"]).toHaveLength(36);
    expect(request.postDataJSON()).toEqual({
      conversation_id: "conversation-1",
      book_id: 7,
      desired_quantity: 1,
      expires_at: "2026-09-25T12:00:00.000Z",
    });
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({
      task: { task_id: "task-1", status: "ACTIVE", book_title: "Agent Systems", desired_quantity: 1, expires_at: "2026-09-25T12:00:00.000Z" },
      replayed: false,
      trace_id: "trace-1",
    }) });
  });
  await page.goto("/");
  await page.getByLabel("输入咨询内容").fill("这本书没货怎么办");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByRole("heading", { name: "创建补货监听" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("confirmation_token");
  await page.getByRole("button", { name: "确认创建监听" }).click();
  await expect(page.getByRole("heading", { name: "补货监听已创建" })).toBeVisible();
  await expect(page.getByText(/不会预留库存/)).not.toBeVisible();
});

test("shows snapshot-only notifications and marks them read", async ({ page }) => {
  const notification = {
    notification_id: "notification-1",
    type: "INVENTORY_AVAILABLE",
    status: "UNREAD",
    title: "补货提醒",
    body: "《Agent Systems》在触发时库存已达到你监听的数量（触发时库存快照：3）。",
    task_id: "task-1",
    payload: { book_id: 7, book_title_snapshot: "Agent Systems", desired_quantity: 1, available_stock: 3 },
    created_at: "2026-08-26T12:00:00.000Z",
    read_at: null,
  };
  await page.route("**/api/notifications?limit=20", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ notifications: [notification], next_cursor: null, trace_id: "trace-1" }) }),
  );
  await page.route("**/api/notifications/notification-1/read", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ notification: { ...notification, status: "READ", read_at: "2026-08-26T12:01:00.000Z" }, replayed: false, trace_id: "trace-2" }) }),
  );
  await page.goto("/notifications");

  await expect(page.getByText(/触发时库存快照：3/)).toBeVisible();
  await expect(page.getByText(/不是持续承诺/)).toBeVisible();
  await page.getByRole("button", { name: "标为已读" }).click();
  await expect(page.getByRole("button", { name: "已读" })).toBeDisabled();
  await expect(page.getByRole("link", { name: "重新查询并购买" })).toHaveAttribute("href", "/?buy_book_id=7");
});

test("lists and cancels an active inventory watch", async ({ page }) => {
  const activeTask = {
    task_id: "task-1",
    status: "ACTIVE",
    book_id: 7,
    book_title: "Agent Systems",
    desired_quantity: 1,
    last_seen_available_stock: 0,
    expires_at: "2026-09-25T12:00:00.000Z",
    created_at: "2026-08-26T12:00:00.000Z",
  };
  await page.route("**/api/tasks?type=INVENTORY_WATCH&limit=20", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tasks: [activeTask], next_cursor: null, trace_id: "trace-1" }) }),
  );
  await page.route("**/api/tasks/task-1/cancel", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ task: { ...activeTask, status: "CANCELLED" }, replayed: false, trace_id: "trace-2" }) }),
  );
  await page.goto("/tasks");

  await expect(page.getByText("《Agent Systems》")).toBeVisible();
  await page.getByRole("button", { name: "取消监听" }).click();
  await expect(page.getByRole("button", { name: "已取消" })).toBeDisabled();
});

test("remains usable on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByLabel("输入咨询内容")).toBeVisible();
  await expect(page.getByRole("navigation")).toBeVisible();
});
