import { expect, test } from "@playwright/test";
import { formatRunTokens, formatTokens, formatUserQuestion, TraceDetail, TraceRun } from "../src/lib/trace";

const sourceRun: TraceRun = {
  run_id: "11111111-1111-4111-8111-111111111111",
  run_kind: "LIVE",
  workflow_type: "CHAT",
  status: "COMPLETED",
  trace_status: "COMPLETE",
  conversation_id: "conversation-1",
  model_profile: "deepseek-v4-flash",
  prompt_set_version: "single-agent-v2",
  tool_registry_version: "catalog-rag-v2",
  replayable: false,
  token_usage: { prompt: 100, completion: 40, cached: null, reasoning: null },
  usage_status: "COMPLETE",
  cost: { status: "UNAVAILABLE", known_microusd: null, currency: "USD" },
  wall_duration_ms: 1200,
  active_duration_ms: 900,
  retry_count: 0,
  created_at: "2026-08-27T00:00:00.000Z",
  completed_at: "2026-08-27T00:00:01.200Z",
  user_question: "推荐一本书",
  input: { content: "推荐一本书" },
  final_answer: "基于 Evidence 推荐一本书。",
  error: null,
};

function detail(run: TraceRun = sourceRun): TraceDetail {
  return {
    run,
    steps: [{
      step_id: "step-1", sequence_no: 1, node_id: "catalog", step_type: "TOOL", agent_name: "single-agent", tool_name: "search_books", status: "COMPLETED", attempt_no: 1,
      input: { q: "distributed" }, output: { books: [{ book_id: 7 }] }, evidence_ids: ["book:7:v2"], model_profile: null, prompt_version: null,
      tool_version: "search_books-v2", token_usage: { prompt: 0, completion: 0, cached: 0, reasoning: 0 }, usage_status: "NO_CALLS",
      cost: { status: "UNAVAILABLE", known_microusd: null, currency: "USD" }, error: null,
      started_at: "2026-08-27T00:00:00.000Z", completed_at: "2026-08-27T00:00:00.100Z", duration_ms: 100,
    }],
    evidence: [{ evidence_id: "book:7:v2", source_ref: "book-guide:7:hash", payload: { book_id: 7, section: "适读人群" } }],
    trace_id: "safe-request-trace",
  };
}

test("trace formatters preserve unknown and zero usage states", () => {
  const zero = { ...sourceRun, token_usage: { prompt: 0, completion: 0, cached: null, reasoning: null } };
  expect(formatRunTokens(zero)).toBe("0");
  expect(formatRunTokens({ ...zero, usage_status: "NO_CALLS" })).toBe("0（无模型调用）");
  expect(formatRunTokens({ ...zero, status: "RUNNING" })).toBe("运行中，汇总未定");
  expect(formatTokens({ ...zero, usage_status: "PARTIAL", token_usage: { ...zero.token_usage, completion: null } })).toBe("未知/未完整上报");
  expect(formatUserQuestion({ ...sourceRun, user_question: null })).toContain("未保留");
});

test("lists only live summaries and exposes no replay or DAG entry", async ({ page }) => {
  await page.route("**/api/traces?*", (route) => route.fulfill({ json: { runs: [sourceRun], next_cursor: null } }));
  await page.goto("/traces");
  await expect(page.getByRole("heading", { name: "执行轨迹" })).toBeVisible();
  await expect(page.getByText("LIVE · CHAT")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("Replay");
  await expect(page.locator("body")).not.toContainText("回放");
  await expect(page.locator("body")).not.toContainText("DAG");
});

test("detail shows ordered steps and safe evidence without hidden fields", async ({ page }) => {
  await page.route(`**/api/traces/${sourceRun.run_id}?*`, (route) => route.fulfill({ json: detail() }));
  await page.goto(`/traces/${sourceRun.run_id}`);
  await expect(page.getByText("基于 Evidence 推荐一本书。")).toBeVisible();
  await expect(page.getByRole("heading", { name: "用户问题" })).toBeVisible();
  await expect(page.getByText("执行时间线")).toBeVisible();
  await page.getByLabel("开发者模式").check();
  await expect(page.getByText(/TOOL · single-agent/)).toBeVisible();
  await expect(page.getByText("book-guide:7:hash")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("confirmation_token");
  await expect(page.locator("body")).not.toContainText("Chain-of-Thought");
});
