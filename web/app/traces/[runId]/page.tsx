"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import type { TraceDetail, TraceRun } from "@/lib/trace";
import { formatCost, formatDuration, formatRunTokens, formatTokens, formatUserQuestion } from "@/lib/trace";

export default function TraceDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = use(params);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [replays, setReplays] = useState<TraceRun[]>([]);
  const [developerMode, setDeveloperMode] = useState(false);
  const [error, setError] = useState("");
  const [confirmReplay, setConfirmReplay] = useState(false);
  const [modelProfile, setModelProfile] = useState("");
  const [promptVersion, setPromptVersion] = useState("");
  const [replayBusy, setReplayBusy] = useState(false);
  const [createdReplay, setCreatedReplay] = useState<string | null>(null);
  const [compareLeft, setCompareLeft] = useState("");
  const [compareRight, setCompareRight] = useState("");
  const [comparison, setComparison] = useState<[TraceDetail, TraceDetail] | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      const response = await fetch(`/api/traces/${encodeURIComponent(runId)}?step_limit=200`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message ?? "轨迹详情加载失败");
      setDetail(payload);
      const sourceId = payload.run.run_kind === "LIVE" ? payload.run.run_id : payload.run.source_run_id;
      if (sourceId) {
        const replayResponse = await fetch(`/api/traces/${encodeURIComponent(sourceId)}/replays?limit=50`, { cache: "no-store" });
        const replayPayload = await replayResponse.json();
        if (replayResponse.ok) setReplays(replayPayload.runs ?? []);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "轨迹详情加载失败");
    }
  }, [runId]);

  useEffect(() => { void load(); }, [load]);

  const sourceRunId = detail?.run.run_kind === "LIVE" ? detail.run.run_id : detail?.run.source_run_id;
  const compareOptions = useMemo(() => {
    if (!detail || !sourceRunId) return [];
    const source: TraceRun = detail.run.run_id === sourceRunId
      ? detail.run
      : { ...detail.run, run_id: sourceRunId, run_kind: "LIVE", source_run_id: null };
    return [source, ...replays].filter((item, index, values) => values.findIndex((other) => other.run_id === item.run_id) === index);
  }, [detail, replays, sourceRunId]);

  async function createReplay() {
    if (!detail) return;
    setReplayBusy(true); setError("");
    try {
      const response = await fetch(`/api/traces/${encodeURIComponent(detail.run.run_id)}/replays`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ model_profile: modelProfile, prompt_set_version: promptVersion }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message ?? "固定结果回放创建失败");
      setCreatedReplay(payload.run.run_id); setConfirmReplay(false);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "固定结果回放创建失败");
    } finally { setReplayBusy(false); }
  }

  async function compare() {
    if (!compareLeft || !compareRight || compareLeft === compareRight) return;
    setError("");
    try {
      const responses = await Promise.all([compareLeft, compareRight].map((id) => fetch(`/api/traces/${encodeURIComponent(id)}?step_limit=200`, { cache: "no-store" })));
      const payloads = await Promise.all(responses.map((response) => response.json()));
      if (!responses.every((response) => response.ok)) throw new Error("对比轨迹加载失败");
      const roots = payloads.map((payload) => payload.run.run_kind === "LIVE" ? payload.run.run_id : payload.run.source_run_id);
      if (roots[0] !== roots[1]) throw new Error("只能对比同一个 LIVE 源及其回放");
      setComparison(payloads as [TraceDetail, TraceDetail]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "对比失败"); }
  }

  async function deleteTrace() {
    if (!window.confirm("删除后轨迹会立即隐藏，并进入延迟清理。确定继续吗？")) return;
    const response = await fetch(`/api/traces/${encodeURIComponent(runId)}`, { method: "DELETE" });
    if (response.ok) window.location.assign("/traces");
    else {
      const payload = await response.json();
      setError(payload?.error?.message ?? "删除失败");
    }
  }

  if (error && !detail) return <main className="trace-page"><div className="error-banner" role="alert">{error} · <Link href="/traces">返回列表</Link></div></main>;
  if (!detail) return <main className="trace-page"><p className="empty-copy">正在加载轨迹详情…</p></main>;
  const run = detail.run;

  return (
    <main className="trace-page">
      <section className="trace-card trace-detail-card">
        <div className="trace-title-row">
          <div><span className="eyebrow">Safe application trace</span><h1>轨迹详情</h1><p className="trace-id">{run.run_id}</p></div>
          <div className="trace-actions">
            <label className="toggle"><input aria-label="开发者模式" type="checkbox" checked={developerMode} onChange={(event) => setDeveloperMode(event.target.checked)} />开发者模式</label>
            <button className="secondary" type="button" onClick={() => void deleteTrace()}>删除轨迹</button>
          </div>
        </div>
        {error ? <div className="error-banner" role="alert">{error}</div> : null}
        {createdReplay ? <div className="success-banner">回放已排队。<Link href={`/traces/${createdReplay}`}>查看新轨迹</Link></div> : null}
        <div className="metric-grid">
          <Metric label="运行状态" value={`${run.status} / ${run.trace_status}`} />
          <Metric label="总耗时" value={formatDuration(run.wall_duration_ms)} />
          <Metric label="活动耗时" value={formatDuration(run.active_duration_ms)} />
          <Metric label="成本" value={formatCost(run.cost)} />
        </div>
        {run.error ? <div className="error-banner">{run.error.code}: {run.error.message}</div> : null}
        <section className="trace-section" aria-labelledby="user-question-title">
          <h2 id="user-question-title">用户问题</h2>
          <p className="trace-question">{formatUserQuestion(run)}</p>
          <small className="muted-text">仅展示已保存的脱敏内容，不包含隐藏提示词。{run.run_kind === "REPLAY" && run.source_run_id ? <> 问题来自<Link href={`/traces/${run.source_run_id}`}>源轨迹</Link>。</> : null}</small>
        </section>
        <section className="trace-section"><h2>最终回答</h2><p className="final-answer">{run.final_answer ?? "该 Run 尚未生成最终回答。"}</p></section>
        <section className="trace-section"><h2>DAG 摘要</h2><p>{detail.steps.length} 个已记录步骤 · {detail.evidence.length} 条 Evidence · {detail.fixture_summary.count} 个固定结果 Fixture</p></section>

        {run.replayable ? <section className="trace-section replay-panel"><h2>固定结果回放</h2><p>可以比较命名模型配置；业务工具只读取源 Run 的安全 Fixture。</p><button className="primary" type="button" onClick={() => { setModelProfile(run.model_profile); setPromptVersion(run.prompt_set_version); setConfirmReplay(true); }}>固定结果回放</button></section> : null}

        {developerMode ? <DeveloperTrace detail={detail} /> : null}

        {compareOptions.length > 1 ? <section className="trace-section"><h2>同源对比</h2><div className="compare-controls">
          <select aria-label="对比轨迹 A" value={compareLeft} onChange={(event) => setCompareLeft(event.target.value)}><option value="">选择轨迹 A</option>{compareOptions.map((item) => <option key={item.run_id} value={item.run_id}>{item.run_kind} · {item.run_id.slice(0, 8)}</option>)}</select>
          <select aria-label="对比轨迹 B" value={compareRight} onChange={(event) => setCompareRight(event.target.value)}><option value="">选择轨迹 B</option>{compareOptions.map((item) => <option key={item.run_id} value={item.run_id}>{item.run_kind} · {item.run_id.slice(0, 8)}</option>)}</select>
          <button className="secondary" type="button" disabled={!compareLeft || !compareRight || compareLeft === compareRight} onClick={() => void compare()}>开始对比</button>
        </div>{comparison ? <TraceComparison values={comparison} /> : null}</section> : null}
      </section>

      {confirmReplay ? <div className="modal-backdrop" role="presentation"><section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="replay-title">
        <h2 id="replay-title">确认固定结果回放</h2>
        <p>回放会调用所选模型并可能产生费用，但不会调用真实业务工具或写入订单、库存、任务、通知或记忆。</p>
        <label className="form-field">模型配置<select value={modelProfile} onChange={(event) => setModelProfile(event.target.value)}>
          {Array.from(new Set([run.model_profile, "deepseek-flash-v1", "deepseek-pro-v1", "default", "judge-default"])).map((profile) => <option key={profile} value={profile}>{profile}</option>)}
        </select></label>
        <label className="form-field">Prompt 版本<select value={promptVersion} onChange={(event) => setPromptVersion(event.target.value)}>
          {Array.from(new Set([run.prompt_set_version, "v2-deepseek-json", "v1", "recommendation-v1"])).map((version) => <option key={version} value={version}>{version}</option>)}
        </select></label>
        <p>默认保留源 Run 的版本；选择 Pro 或更换 Prompt 必须由你显式确认。历史占位模型不会自动映射到真实模型。</p>
        <div className="modal-actions"><button className="secondary" type="button" onClick={() => setConfirmReplay(false)}>取消</button><button className="primary" type="button" disabled={replayBusy} onClick={() => void createReplay()}>{replayBusy ? "正在创建…" : "确认并创建回放"}</button></div>
      </section></div> : null}
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong></div>; }

function DeveloperTrace({ detail }: { detail: TraceDetail }) {
  const run = detail.run;
  return <section className="trace-section developer-trace"><h2>开发者安全视图</h2><div className="metric-grid compact"><Metric label="模型 Profile" value={run.model_profile} /><Metric label="Prompt 版本" value={run.prompt_set_version} /><Metric label="工具版本" value={run.tool_registry_version.slice(0, 12)} /><Metric label="重试" value={String(run.retry_count)} /></div><p>Token：{formatRunTokens(run)}；prompt {run.token_usage.prompt ?? "未知"} / completion {run.token_usage.completion ?? "未知"} / cached {run.token_usage.cached ?? "—"} / reasoning {run.token_usage.reasoning ?? "—"}</p><div className="timeline">{detail.steps.map((step) => <article className="timeline-step" key={step.step_id}><div><span className="sequence">#{step.sequence_no}</span><strong>{step.step_type} · {step.agent_name ?? step.tool_name ?? step.node_id}</strong><small>{step.status} · {formatDuration(step.duration_ms)} · attempt {step.attempt_no}</small></div><p>本步骤直接调用 Token：{formatTokens(step)}</p><details><summary>安全投影与 Evidence</summary><pre>{JSON.stringify({ input: step.input, output: step.output, evidence_ids: step.evidence_ids, error: step.error }, null, 2)}</pre></details></article>)}</div>{detail.evidence.length ? <div className="evidence-view"><h3>Evidence 安全投影</h3><pre>{JSON.stringify(detail.evidence, null, 2)}</pre></div> : null}</section>;
}

function TraceComparison({ values }: { values: [TraceDetail, TraceDetail] }) {
  return <div className="comparison-grid">{values.map((detail) => <article key={detail.run.run_id}><h3>{detail.run.run_kind} · {detail.run.run_id.slice(0, 8)}</h3><p>{detail.run.final_answer ?? "无最终回答"}</p><dl><dt>步骤</dt><dd>{detail.steps.length}</dd><dt>Token</dt><dd>{formatRunTokens(detail.run)}</dd><dt>成本</dt><dd>{formatCost(detail.run.cost)}</dd><dt>延迟</dt><dd>{formatDuration(detail.run.wall_duration_ms)}</dd></dl></article>)}</div>;
}
