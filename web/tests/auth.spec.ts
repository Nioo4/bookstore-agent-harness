import { expect, test } from "@playwright/test";

test.use({ trace: "off", screenshot: "off", video: "off" });

test("navigation shows the session display name and performs logout", async ({ page }) => {
  let signedOut = false;
  let logoutCalls = 0;
  await page.route("**/api/session", (route) => route.fulfill({
    json: signedOut
      ? { authenticated: false, user: null }
      : { authenticated: true, user: { display_name: "测试读者" } },
  }));
  await page.route("**/api/auth/logout", async (route) => {
    logoutCalls++;
    signedOut = true;
    await route.fulfill({ json: { ok: true } });
  });

  await page.goto("/");
  const trigger = page.getByRole("button", { name: "测试读者" });
  await expect(trigger).toBeVisible();
  await trigger.press("Enter");
  await expect(page.getByRole("menuitem", { name: "登出" })).toBeVisible();
  await trigger.press("Escape");
  await expect(page.getByRole("menuitem", { name: "登出" })).toHaveCount(0);
  await trigger.hover();
  const triggerBox = await trigger.boundingBox();
  expect(triggerBox).not.toBeNull();
  await page.mouse.move(triggerBox!.x + triggerBox!.width / 2, triggerBox!.y + triggerBox!.height + 4);
  await expect(page.getByRole("menuitem", { name: "登出" })).toBeVisible();
  await page.getByRole("menuitem", { name: "登出" }).click();
  await expect(page.getByRole("link", { name: "登录" })).toBeVisible();
  expect(logoutCalls).toBe(1);
});

test("navigation and consultation boundary share one session response", async ({ page }) => {
  let sessionCalls = 0;
  await page.route("**/api/session", route => {
    sessionCalls++;
    return route.fulfill({ json: { authenticated: true, user: { display_name: "统一会话" } } });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "统一会话" })).toBeVisible();
  await expect(page.getByText("当前会话已登录：", { exact: false })).toBeVisible();
  expect(sessionCalls).toBe(1);
});

for (const scenario of ["SMTP_ACCEPTED", "ALREADY_VERIFIED", "RATE_LIMITED"]) {
  test(`resend UI handles ${scenario} without automatic retry`, async ({ page }) => {
    await page.clock.install();
    await page.route("**/api/auth/login", route => route.fulfill({ json: { user: { email_verified: false } } }));
    let calls = 0;
    await page.route("**/api/auth/resend-verification", async route => {
      calls++;
      await route.fulfill(scenario === "RATE_LIMITED" ? { status: 429, headers: { "Retry-After": "125" }, json: { error: { code: "AUTH_RATE_LIMITED" } } } :
        { status: scenario === "ALREADY_VERIFIED" ? 200 : 202, json: { status: scenario, verification_email_sent: scenario === "SMTP_ACCEPTED", retry_after_seconds: scenario === "ALREADY_VERIFIED" ? 0 : 60 } });
    });
    await page.goto("/login");
    await page.getByLabel("邮箱", { exact: true }).fill("synthetic@example.test");
    await page.getByLabel("密码", { exact: true }).fill("test-password-only");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.getByRole("button", { name: "重发验证邮件", exact: true }).click();
    if (scenario === "RATE_LIMITED") await expect(page.getByRole("button", { name: "125 秒后可尝试重发" })).toBeDisabled();
    else if (scenario === "ALREADY_VERIFIED") await expect(page.getByRole("status")).toContainText("重新登录以更新登录凭证");
    else await expect(page.getByRole("status")).toContainText("不保证已到达收件箱");
    await page.clock.fastForward(126_000);
    expect(calls).toBe(1);
  });
}

test("SMTP failure does not claim a verification email was sent or repeat registration", async ({ page }) => {
  let requests = 0;
  await page.route("**/api/auth/register", async (route) => {
    requests++;
    await route.fulfill({ status: 202, json: { user_id: 1, email: "synthetic@example.test", verification_email_sent: false } });
  });
  await page.goto("/register");
  await page.getByLabel("显示名称").fill("Synthetic");
  await page.getByLabel("邮箱", { exact: true }).fill("synthetic@example.test");
  await page.getByLabel("密码", { exact: true }).fill("test-password-only");
  await page.getByRole("button", { name: "注册并发送验证邮件" }).click();
  await expect(page.locator(".error-banner")).toContainText("账号已创建，但无法确认验证邮件发送");
  await expect(page.getByRole("link", { name: "登录后重发验证邮件" })).toBeVisible();
  await expect(page.getByText("验证邮件已发送。", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "注册并发送验证邮件" })).toBeDisabled();
  expect(requests).toBe(1);
});

test("unverified login requires explicit resend and honors cooldown without retries", async ({ page }) => {
  await page.clock.install();
  await page.route("**/api/auth/login", route => route.fulfill({ json: { user: { email_verified: false } } }));
  let requests = 0;
  await page.route("**/api/auth/resend-verification", async route => {
    requests++;
    expect(route.request().postData()).toBe("{}");
    await route.fulfill({ status: 202, json: { status: "DELIVERY_UNCONFIRMED", verification_email_sent: false, retry_after_seconds: 60 } });
  });
  await page.goto("/login");
  await page.getByLabel("邮箱", { exact: true }).fill("synthetic@example.test");
  await page.getByLabel("密码", { exact: true }).fill("test-password-only");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "重发验证邮件", exact: true })).toBeEnabled();
  expect(requests).toBe(0);
  await page.getByRole("button", { name: "重发验证邮件", exact: true }).click();
  await expect(page.getByRole("button", { name: "60 秒后可尝试重发" })).toBeDisabled();
  await expect(page.getByRole("status")).toContainText("原有效链接仍可使用");
  await page.clock.fastForward(60_001);
  await expect(page.getByRole("button", { name: "重发验证邮件", exact: true })).toBeEnabled();
  expect(requests).toBe(1);
});

test("expired verification clears query and submits once across development remounts", async ({ page }) => {
  let requests = 0;
  await page.route("**/api/auth/verify-email", async route => {
    requests++;
    await route.fulfill({ status: 409, json: { error: { code: "EMAIL_VERIFICATION_EXPIRED" } } });
  });
  const response = await page.goto("/verify-email?token=synthetic-test-only");
  await expect(page).toHaveURL(/\/verify-email$/);
  await expect(page.getByRole("status")).toContainText("验证链接已过期");
  await expect(page.getByRole("link", { name: "登录后重发 / 确认验证状态" })).toBeVisible();
  expect(requests).toBe(1);
  expect(await page.evaluate(() => Object.keys(localStorage).length)).toBe(0);
});

test("registration network failure leaves a recoverable form and an uncertain-result warning", async ({ page }) => {
  await page.route("**/api/auth/register", (route) => route.abort());
  await page.goto("/register");
  await page.getByLabel("显示名称").fill("Synthetic");
  await page.getByLabel("邮箱", { exact: true }).fill("synthetic@example.test");
  await page.getByLabel("密码", { exact: true }).fill("test-password-only");
  await page.getByRole("button", { name: "注册并发送验证邮件" }).click();
  await expect(page.locator(".error-banner")).toContainText("未取得注册结果");
  await expect(page.getByRole("button", { name: "注册并发送验证邮件" })).toBeEnabled();
});
