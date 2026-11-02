import { expect, test } from "@playwright/test";

test("serves a direct trace deep link through the static SPA fallback", async ({ page }) => {
  const runId = "33333333-3333-4333-8333-333333333333";
  await page.route(`**/api/traces/${runId}?*`, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      run: {
        run_id: runId,
        run_kind: "LIVE",
        workflow_type: "CHAT",
        status: "COMPLETED",
        trace_status: "COMPLETE",
        model_profile: "deepseek-v4-flash",
        prompt_set_version: "single-agent-v2",
        tool_registry_version: "catalog-rag-v2",
        token_usage: { prompt: 1, completion: 1, cached: null, reasoning: null },
        usage_status: "COMPLETE",
        cost: { status: "UNAVAILABLE", known_microusd: null, currency: "USD" },
        wall_duration_ms: 10,
        retry_count: 0,
        created_at: "2026-09-25T00:00:00.000Z",
        user_question: "直接打开轨迹",
        final_answer: "SPA 深链接已加载。",
        error: null,
      },
      steps: [],
      evidence: [],
      trace_id: "trace-deep-link",
    }),
  }));

  const response = await page.goto(`/traces/${runId}`);
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole("heading", { name: "轨迹详情", exact: true })).toBeVisible();
  await expect(page.getByText("SPA 深链接已加载。", { exact: true })).toBeVisible();
});
