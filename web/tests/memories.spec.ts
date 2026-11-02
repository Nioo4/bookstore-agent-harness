import { expect, test } from "@playwright/test";

const memory = {
  memory_id: "test-language", memory_key: "preference.languages", memory_type: "PREFERENCE",
  value: { items: ["zh-CN"] }, source: "EXPLICIT",
};

test("memory management requires confirmation and removes only successful deletes", async ({ page }) => {
  let deleted = false;
  await page.route("**/api/memories**", async (route) => {
    if (route.request().method() === "DELETE") {
      deleted = true;
      await route.fulfill({ json: { deleted: true } });
    } else await route.fulfill({ json: { memories: [memory] } });
  });
  await page.goto("/memories");
  await expect(page.getByText("偏好语言", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "删除这条记忆" }).click();
  expect(deleted).toBe(false);
  await expect(page.getByText("zh-CN", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除这条记忆" }).click();
  await expect(page.getByText("暂无已保存的记忆。")).toBeVisible();
  expect(deleted).toBe(true);
});

test("failed memory deletion preserves records and allows retry", async ({ page }) => {
  let fail = true;
  await page.route("**/api/memories**", async (route) => {
    if (route.request().method() === "DELETE") await route.fulfill({ status: fail ? 503 : 200, json: {} });
    else await route.fulfill({ json: { memories: [memory] } });
  });
  await page.goto("/memories");
  await expect(page.getByText("zh-CN", { exact: true })).toBeVisible();
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除全部记忆" }).click();
  await expect(page.locator(".error-banner")).toContainText("删除未成功");
  await expect(page.getByText("zh-CN", { exact: true })).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "删除全部记忆" }).click();
  await expect(page.getByText("暂无已保存的记忆。")).toBeVisible();
});

test("v2 wrapped preference value is visible and human readable", async ({ page }) => {
  await page.route("**/api/memories**", route => route.fulfill({ json: { memories: [{
    memory_id: "v2-language", key: "PREFERRED_LANGUAGE", memory_type: "PREFERENCE",
    value: { value: "zh-CN" }, source: "EXPLICIT",
  }] } }));
  await page.goto("/memories");
  await expect(page.getByText("中文（zh-CN）", { exact: true })).toBeVisible();
});
