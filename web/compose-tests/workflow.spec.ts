import { expect, test } from "@playwright/test";
import { randomBytes, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import path from "node:path";

test("real Compose SMTP recovery, multi-constraint recommendation, order and memory UI", async ({ page, request }) => {
  let stage = "test-project-check";
  let chatStatus = 0;
  let registrationStatus = 0;
  let loginStatus = 0;
  let chatEvents = 0;
  let recommendationCount = -1;
  let recommendedIds: number[] = [];
  let latestChatRun = "";
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/auth/register")) registrationStatus = response.status();
    if (response.url().endsWith("/api/auth/login")) loginStatus = response.status();
    if (!/\/api\/chat\/conversations\/[^/]+\/messages$/.test(response.url())) return;
    chatStatus = response.status();
    try {
      const body = await response.text();
      chatEvents = (body.match(/event:/g) ?? []).length;
      for (const line of body.split(/\r?\n/)) {
        if (!line.startsWith("data:")) continue;
        const data = JSON.parse(line.slice(5));
        if (Array.isArray(data.recommendations)) {
          recommendationCount = data.recommendations.length;
          recommendedIds = data.recommendations.map((item: { book_id: number }) => item.book_id);
          latestChatRun = data.run_id ?? "";
        }
      }
    } catch { /* Diagnostics contain only counts, never payloads. */ }
  });
  try {
    const project = process.env.BOOKSTORE_TEST_PROJECT ?? "bookstore-stage11";
    if (!/^bookstore-(stage11|test)[a-z0-9-]*$/.test(project)) throw new Error("TEST_PROJECT_REQUIRED");
    const compose = (args: string[], input?: string) => execFileSync("docker",
      ["compose", "-p", project, "-f", "compose.yaml", "-f", "compose.e2e.yaml", ...args],
      { cwd: path.resolve(__dirname, "../.."), input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], timeout: 30_000 }).trim();
    const sql = (input: string) => compose(["exec", "-T", "mysql", "sh", "-c",
      'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -N -B -uroot -D "$MYSQL_DATABASE"'], input);
    stage = "ingress-check";
    expect(compose(["port", "nginx", "80"])).toBe("127.0.0.1:8088");
    stage = "fixture-check";
    compose(["exec", "-T", "agent-api", "python", "-c",
      "import os; assert os.environ['MODEL_BASE_URL']=='http://model-fixture:8010/v1'; assert os.environ['ENVIRONMENT']=='test'"]);
    stage = "fixture-seed";
    const unique = randomUUID().replaceAll("-", "").slice(0, 16).replace(/[0-9]/g, digit => "ghijklmnop"[Number(digit)]);
    const email = `browser.${unique}@example.test`;
    const password = randomBytes(20).toString("hex");
    const keyword = `Browser E2E Book ${unique}`;
    const bookId = Number(sql(`INSERT INTO book(isbn,title,price,stock_count,language_code) VALUES('browser-${unique}','${keyword}',4200,8,'en'); SET @book=LAST_INSERT_ID(); INSERT INTO inventory(book_id,available_stock) VALUES(@book,8); SELECT @book;`));
    expect(Number.isSafeInteger(bookId)).toBe(true);
    for (const [suffix, price, stock, language] of [["cost", 5200, 8, "en"], ["lang", 4200, 8, "zh-CN"], ["empty", 4200, 0, "en"]]) {
      sql(`INSERT INTO book(isbn,title,price,stock_count,language_code) VALUES('browser-${suffix}-${unique}','${keyword} ${suffix}',${price},${stock},'${language}'); INSERT INTO inventory(book_id,available_stock) VALUES(LAST_INSERT_ID(),${stock});`);
    }

    stage = "register-navigation";
    await page.goto("/register");
    stage = "register-name";
    await page.getByLabel("显示名称").fill("Browser E2E");
    stage = "register-email";
    await page.getByLabel("邮箱", { exact: true }).fill(email);
    stage = "register-password";
    await page.getByLabel("密码", { exact: true }).fill(password);
    stage = "register-submit";
    try {
      compose(["stop", "mailpit"]);
      await page.getByRole("button", { name: "注册并发送验证邮件" }).click();
      stage = "register-result";
      await expect(page.locator(".error-banner")).toContainText("账号已创建，但无法确认验证邮件发送");
      await expect(page.getByRole("button", { name: "注册并发送验证邮件" })).toBeDisabled();
    } finally {
      compose(["start", "mailpit"]);
    }
    stage = "explicit-resend-with-lost-response";
    await page.getByRole("link", { name: "登录后重发验证邮件" }).click();
    await expect(page.getByRole("heading", { name: "继续你的阅读旅程" })).toBeVisible();
    await page.getByLabel("邮箱", { exact: true }).fill(email);
    await page.getByLabel("密码", { exact: true }).fill(password);
    const firstLoginResponse = page.waitForResponse(response => response.url().endsWith("/api/auth/login"));
    await page.getByRole("button", { name: "登录", exact: true }).click();
    expect((await firstLoginResponse).status()).toBe(200);
    const resend = page.getByRole("button", { name: "重发验证邮件", exact: true });
    await expect(resend).toBeEnabled();
    let resendRequests = 0;
    let serverResendStatus = 0;
    page.on("request", req => { if (req.url().endsWith("/api/auth/resend-verification")) resendRequests++; });
    await page.route("**/api/auth/resend-verification", async route => {
      // Execute the real backend/SMTP request, then drop only its browser response.
      serverResendStatus = (await route.fetch({ maxRetries: 0 })).status();
      await route.abort("failed");
    }, { times: 1 });
    await resend.click();
    await expect(page.getByRole("status")).toContainText("未取得重发结果；邮件可能已经发送");
    expect(serverResendStatus).toBe(202);
    expect(resendRequests).toBe(1);
    const limitedResponse = page.waitForResponse(response => response.url().endsWith("/api/auth/resend-verification"));
    await resend.click();
    expect((await limitedResponse).status()).toBe(429);
    await expect(page.getByRole("status")).toContainText("重发次数受到限制");
    expect(resendRequests).toBe(2);

    stage = "mail-verification";
    let token = "";
    await expect.poll(async () => {
      const messages = await (await request.get("http://127.0.0.1:8025/api/v1/messages")).json();
      const message = messages.messages.find((item: { To: Array<{ Address: string }> }) => item.To.some((recipient) => recipient.Address === email));
      if (!message) return false;
      const letter = await (await request.get(`http://127.0.0.1:8025/api/v1/message/${message.ID}`)).json();
      token = /token=([A-Za-z0-9_-]+)/.exec(letter.Text)?.[1] ?? "";
      return Boolean(token);
    }).toBe(true);
    await page.goto(`/verify-email?token=${token}`);
    await expect(page.getByRole("heading", { name: "验证完成" })).toBeVisible();

    stage = "login";
    await page.getByRole("link", { name: "重新登录", exact: true }).click();
    await expect(page.getByRole("heading", { name: "继续你的阅读旅程" })).toBeVisible();
    await page.getByLabel("邮箱", { exact: true }).fill(email);
    await page.getByLabel("密码", { exact: true }).fill(password);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByLabel("输入咨询内容")).toBeVisible();
    expect((await page.context().cookies()).some((cookie) => cookie.name === "bookstore_access" && cookie.httpOnly)).toBe(true);
    expect(await page.evaluate(() => document.cookie.includes("bookstore_access"))).toBe(false);

    stage = "recommendation";
    await page.getByLabel("输入咨询内容").fill(`推荐一本英文有货书，预算50元以内，书名包含「${keyword}」`);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    stage = "recommendation-card";
    await expect(page.locator(".book-card").first()).toBeVisible();
    stage = "recommendation-finished";
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();
    await expect.poll(() => recommendedIds).toEqual([bookId]);
    const recommendationRun = latestChatRun;
    expect(recommendationRun).toMatch(/^[0-9a-f-]{36}$/);
    expect(sql(`SELECT price,language_code,available_stock FROM book JOIN inventory USING(book_id) WHERE book_id=${bookId};`)).toBe("4200\ten\t8");
    stage = "confirmation";
    await page.getByLabel("输入咨询内容").fill(`购买书籍 ${bookId}`);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByRole("heading", { name: "订单预览" })).toBeVisible();
    expect(Number(sql(`SELECT COUNT(*) FROM order_item WHERE book_id=${bookId};`))).toBe(0);
    await page.getByRole("button", { name: "确认并创建模拟订单" }).click();
    await expect(page.getByRole("heading", { name: "模拟订单已创建" })).toBeVisible();
    expect(Number(sql(`SELECT COUNT(*) FROM order_item WHERE book_id=${bookId};`))).toBe(1);
    expect(Number(sql(`SELECT available_stock FROM inventory WHERE book_id=${bookId};`))).toBe(7);
    stage = "order-history";
    await page.getByRole("link", { name: "我的订单", exact: true }).click();
    await expect(page.locator(".order-row")).toContainText("Browser E2E Book");

    stage = "memory";
    await page.getByRole("link", { name: "我的记忆", exact: true }).click();
    await expect(page.getByRole("heading", { name: "我的记忆", exact: true })).toBeVisible();
    await page.getByLabel("偏好类型").selectOption("PREFERRED_LANGUAGE");
    await page.getByLabel("偏好内容").fill("en");
    const preferenceResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/memories/preferences"));
    await page.getByRole("button", { name: "保存明确偏好", exact: true }).click();
    expect((await preferenceResponse).status()).toBe(200);
    await expect(page.getByText("偏好语言", { exact: true })).toBeVisible();
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "删除全部记忆" }).click();
    await expect(page.getByText("暂无已保存的记忆。")).toBeVisible();

    stage = "traces";
    await page.getByRole("link", { name: "执行轨迹", exact: true }).click();
    await expect(page.locator(".trace-row").first()).toBeVisible();
    await page.locator(".trace-row").first().click();
    await expect(page.getByRole("heading", { name: "轨迹详情", exact: true })).toBeVisible();
    stage = "trace-evidence";
    await page.goto(`/traces/${recommendationRun}`);
    await page.getByLabel("开发者模式").check();
    await expect(page.getByRole("heading", { name: "Evidence 安全投影" })).toBeVisible();
    await expect(page.locator("body")).not.toContainText("固定结果回放");
    await expect(page.locator("body")).not.toContainText("DAG");
  } catch (error) {
    // Do not forward Playwright's raw call logs, input values or verification URL.
    const failureLine = error instanceof Error ? /workflow\.spec\.ts:(\d+):\d+/.exec(error.stack ?? "")?.[1] ?? "unknown" : "unknown";
    await page.goto("about:blank").catch(() => {});
    throw new Error(`COMPOSE_BROWSER_FAILED_${stage}_line${failureLine}_register${registrationStatus}_login${loginStatus}_http${chatStatus}_events${chatEvents}_recommendations${recommendationCount}`);
  }
});

test("observability profile Grafana dashboard renders current Prometheus numeric samples", async ({ page }) => {
  test.skip(process.env.BOOKSTORE_OBSERVABILITY_PROFILE !== "1", "requires docker compose --profile observability and BOOKSTORE_OBSERVABILITY_PROFILE=1");
  let stage = "project-check";
  try {
    const project = process.env.BOOKSTORE_TEST_PROJECT ?? "bookstore-stage11";
    if (!/^bookstore-(stage11|test)[a-z0-9-]*$/.test(project)) throw new Error("TEST_PROJECT_REQUIRED");
    const compose = (args: string[]) => execFileSync("docker",
      ["compose", "-p", project, "-f", "compose.yaml", "-f", "compose.e2e.yaml", ...args],
      { cwd: path.resolve(__dirname, "../.."), encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], timeout: 15_000 }).trim();
    stage = "grafana-port";
    expect(compose(["port", "grafana", "3000"])).toBe("127.0.0.1:3001");
    const container = compose(["ps", "-q", "grafana"]);
    const details = JSON.parse(execFileSync("docker", ["inspect", container], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] }))[0];
    const environment = Object.fromEntries((details.Config.Env as string[]).map(item => {
      const index = item.indexOf("=");
      return [item.slice(0, index), item.slice(index + 1)];
    }));
    let observedNumericSample = false;
    page.on("response", async response => {
      if (!response.url().includes("/api/ds/query") || !response.ok()) return;
      try {
        const queries = response.request().postDataJSON().queries;
        if (!queries.some((query: { datasource?: { uid?: string } }) => query.datasource?.uid === "prometheus")) return;
        const result = await response.json();
        for (const value of Object.values(result.results) as Array<{ frames?: Array<{ schema: { fields: Array<{ type: string }> }, data: { values: unknown[][] } }> }>) {
          for (const frame of value.frames ?? []) {
            frame.schema.fields.forEach((field, index) => {
              if (field.type === "number" && frame.data.values[index]?.some(sample => typeof sample === "number" && Number.isFinite(sample) && sample > 0)) observedNumericSample = true;
            });
          }
        }
      } catch { /* No payloads or credentials in failure diagnostics. */ }
    });
    stage = "grafana-login";
    await page.goto("http://127.0.0.1:3001/login");
    await page.locator('input[name="user"]').fill(environment.GF_SECURITY_ADMIN_USER);
    await page.locator('input[name="password"]').fill(environment.GF_SECURITY_ADMIN_PASSWORD);
    const loginResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/login" && response.request().method() === "POST");
    await page.getByRole("button", { name: /log in|sign in/i }).click();
    expect((await loginResponse).ok()).toBe(true);
    await expect.poll(async () => (await page.context().cookies("http://127.0.0.1:3001")).some(cookie => cookie.name === "grafana_session")).toBe(true);
    await page.waitForURL(url => url.pathname !== "/login");
    stage = "grafana-dashboard";
    await page.goto("http://127.0.0.1:3001/d/bookstore-agent-overview?from=now-30m&to=now");
    stage = "grafana-panel-titles";
    await expect(page.getByText("Chat completion ratio", { exact: true })).toBeVisible();
    await expect(page.getByText("Agent node p95 (seconds)", { exact: true })).toBeVisible();
    stage = "grafana-numeric-samples";
    await expect.poll(() => observedNumericSample, { timeout: 45_000 }).toBe(true);
  } catch {
    await page.goto("about:blank").catch(() => {});
    throw new Error(`COMPOSE_GRAFANA_FAILED_${stage}: no credential, response body or screenshot retained`);
  }
});
