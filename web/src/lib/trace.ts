export type TraceCost = {
  status: "CALCULATED" | "PARTIAL" | "UNAVAILABLE";
  known_microusd: number | null;
  currency: "USD";
  pricing_version?: string | null;
};

export type TraceRun = {
  run_id: string;
  source_run_id?: string | null;
  run_kind?: "LIVE" | "REPLAY";
  workflow_type: string;
  status: string;
  trace_status: "RECORDING" | "COMPLETE" | "INCOMPLETE";
  conversation_id?: string;
  model_profile: string;
  prompt_set_version: string;
  tool_registry_version: string;
  replayable?: boolean;
  token_usage: { prompt: number | null; completion: number | null; cached: number | null; reasoning: number | null };
  usage_status: "NO_CALLS" | "COMPLETE" | "PARTIAL" | "UNAVAILABLE" | "LEGACY_UNKNOWN";
  cost: TraceCost;
  wall_duration_ms: number | null;
  active_duration_ms?: number | null;
  retry_count: number;
  created_at: string;
  completed_at?: string | null;
  user_question?: string | null;
  input?: Record<string, unknown>;
  final_answer?: string | null;
  error?: { code?: string; message?: string } | null;
};

export type TraceStep = {
  step_id: string;
  sequence_no: number;
  step_type: string;
  agent_name?: string | null;
  node_id?: string;
  tool_name?: string | null;
  status: string;
  attempt_no: number;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  evidence_ids: string[];
  model_profile?: string | null;
  prompt_version?: string | null;
  tool_version?: string | null;
  token_usage: TraceRun["token_usage"];
  usage_status: TraceRun["usage_status"];
  cost: TraceCost;
  error: { code?: string; message?: string } | null;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
};

export type TraceDetail = {
  run: TraceRun;
  steps: TraceStep[];
  evidence: Array<Record<string, unknown>>;
  fixture_summary?: { count: number; replayable_count: number };
  trace_id?: string;
};

export function formatUserQuestion(run: TraceRun): string {
  const question = run.user_question === undefined ? run.input?.content : run.user_question;
  if (typeof question === "string" && question.trim()) return question;
  return run.workflow_type === "INVENTORY_WATCH" ? "此后台任务未记录独立的用户问题。" : "该轨迹未保留可展示的用户问题（可能已截断）。";
}

export function formatCost(cost: TraceCost): string {
  if (cost.status === "UNAVAILABLE" || cost.known_microusd === null) return "不可用";
  const amount = (cost.known_microusd / 1_000_000).toFixed(6);
  return `${cost.status === "PARTIAL" ? "已知小计 " : ""}$${amount} USD`;
}

export function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
}

export function formatTokens(value: Pick<TraceRun, "token_usage" | "usage_status">): string {
  if (value.usage_status === "LEGACY_UNKNOWN") return "历史统计未确认";
  if (value.usage_status === "NO_CALLS") return "0（无模型调用）";
  const { prompt, completion } = value.token_usage;
  if (prompt === null || completion === null) return "未知/未完整上报";
  return String(prompt + completion);
}

export function formatRunTokens(run: TraceRun): string {
  if (run.usage_status === "LEGACY_UNKNOWN") return "历史统计未确认";
  if (run.status === "RUNNING" || run.status === "QUEUED") return "运行中，汇总未定";
  return formatTokens(run) + (run.status.startsWith("WAITING_") ? "（截至当前暂停点）" : "");
}
