"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Memory = {
  memory_id: string;
  memory_key: string;
  memory_type: "PREFERENCE" | "NEGATIVE_PREFERENCE" | "EPISODE";
  value: { items?: string[]; currency?: string; max_amount?: number; summary?: string };
  source: "EXPLICIT" | "INFERRED";
};

const names: Record<string, string> = {
  "preference.languages": "偏好语言", "preference.topics": "偏好主题", "preference.authors": "偏好作者",
  "negative.languages": "不偏好的语言", "negative.topics": "不偏好的主题", "negative.authors": "不偏好的作者",
};

function description(memory: Memory) {
  const { value } = memory;
  if (value.items) return value.items.join("、");
  if (typeof value.max_amount === "number") return `${(value.max_amount / 100).toFixed(2)} ${value.currency ?? ""}`;
  return value.summary ?? "";
}

export default function MemoriesPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/memories?limit=100", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error("记忆加载失败，请确认已登录后重试。");
        setMemories(payload.memories ?? []);
      })
      .catch(() => { if (!controller.signal.aborted) setError("记忆加载失败，请确认已登录后重试。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  async function remove(memory?: Memory) {
    if (!window.confirm(memory ? "删除这条记忆？删除后不再用于后续推荐。" : "删除全部记忆？此操作不能撤销。")) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(memory ? `/api/memories/${encodeURIComponent(memory.memory_id)}` : "/api/memories", { method: "DELETE" });
      if (!response.ok) throw new Error("DELETE_FAILED");
      setMemories((items) => memory ? items.filter((item) => item.memory_id !== memory.memory_id) : []);
      setMessage(memory ? "已删除这条记忆。" : "已删除全部记忆。");
    } catch {
      setError("删除未成功，请重试。页面未移除原记录。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="orders-page">
      <section className="orders-card">
        <span className="eyebrow">Your preferences</span>
        <h1>我的记忆</h1>
        <p className="page-intro">查看咨询中保存的偏好。删除后不再用于后续推荐；之后的咨询仍可能产生新记忆。此页最多显示最近 100 条，“删除全部”作用于全部记忆。</p>
        {error ? <div className="error-banner" role="alert">{error} <Link href="/login">前往登录</Link></div> : null}
        {message ? <p role="status">{message}</p> : null}
        <button className="secondary" type="button" disabled={loading || busy || !memories.length} onClick={() => void remove()}>删除全部记忆</button>
        <div className="order-list" aria-busy={loading || busy}>
          {loading ? <p>正在加载记忆…</p> : !error && !memories.length ? <p className="empty-copy">暂无已保存的记忆。</p> : null}
          {memories.map((memory) => (
            <article className="notification-row" key={memory.memory_id}>
              <div>
                <strong>{names[memory.memory_key] ?? (memory.memory_type === "EPISODE" ? "阅读咨询摘要" : "预算偏好")}</strong>
                <p>{description(memory)}</p>
                <small>{memory.source === "EXPLICIT" ? "你明确表达的偏好" : "从咨询中推断，可能不准确"}</small>
              </div>
              <button className="secondary" type="button" disabled={busy} onClick={() => void remove(memory)}>删除这条记忆</button>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
