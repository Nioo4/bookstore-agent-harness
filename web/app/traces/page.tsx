"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { TraceRun } from "@/lib/trace";
import { formatCost, formatDuration, formatRunTokens, formatUserQuestion } from "@/lib/trace";

export default function TracesPage() {
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ limit: "20" });
    if (kind) query.set("kind", kind);
    if (status) query.set("status", status);
    try {
      const response = await fetch(`/api/traces?${query}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message ?? "执行轨迹加载失败");
      setRuns(payload.runs ?? []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "执行轨迹加载失败");
    } finally {
      setLoading(false);
    }
  }, [kind, status]);

  useEffect(() => { void load(); }, [load]);

  return (
    <main className="trace-page">
      <section className="trace-card">
        <span className="eyebrow">Agent execution history</span>
        <h1>执行轨迹</h1>
        <p className="page-intro">查看自己的 LIVE 与固定结果回放。轨迹只展示安全投影，不包含隐藏提示词或模型隐式推理。</p>
        <div className="trace-filters" aria-label="轨迹筛选">
          <label>类型
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="">全部</option><option value="LIVE">LIVE</option><option value="REPLAY">REPLAY</option>
            </select>
          </label>
          <label>状态
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">全部</option>
              {['QUEUED', 'RUNNING', 'WAITING_CONFIRMATION', 'COMPLETED', 'FAILED', 'CANCELLED'].map((value) => <option key={value}>{value}</option>)}
            </select>
          </label>
          <button className="secondary" type="button" onClick={() => void load()}>刷新</button>
        </div>
        {error ? <div className="error-banner" role="alert">{error} · <Link href="/login">前往登录</Link></div> : null}
        {loading ? <p className="empty-copy">正在加载轨迹…</p> : null}
        {!loading && !error && !runs.length ? <p className="empty-copy">还没有可查看的执行轨迹。</p> : null}
        <div className="trace-list">
          {runs.map((run) => (
            <Link className="trace-row" href={`/traces/${run.run_id}`} key={run.run_id}>
              <div className="trace-row-main">
                <span className={`status-pill status-${run.status.toLowerCase()}`}>{run.status}</span>
                <strong>{run.run_kind} · {run.workflow_type}</strong>
                <p className="trace-question-preview">用户问题：{formatUserQuestion(run)}</p>
                <small>{new Date(run.created_at).toLocaleString("zh-CN")} · {run.run_id}</small>
              </div>
              <div className="trace-row-metrics">
                <span>总耗时 {formatDuration(run.wall_duration_ms)}</span>
                <span>Token {formatRunTokens(run)}</span>
                <span>成本 {formatCost(run.cost)}</span>
                <span className={run.trace_status === "INCOMPLETE" ? "danger-text" : "muted-text"}>{run.trace_status}</span>
              </div>
            </Link>
          ))}
        </div>
      </section>
    </main>
  );
}
