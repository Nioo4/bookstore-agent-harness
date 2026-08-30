"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Task = {
  task_id: string;
  status: "ACTIVE" | "TRIGGERED" | "COMPLETED" | "EXPIRED" | "CANCELLED";
  book_id: number;
  book_title: string;
  desired_quantity: number;
  last_seen_available_stock: number;
  expires_at: string;
  created_at: string;
};

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/tasks?type=INVENTORY_WATCH&limit=20", { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.error?.message ?? "监听任务加载失败");
        setTasks(payload.tasks ?? []);
      })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "监听任务加载失败"));
  }, []);

  async function cancel(task: Task) {
    const response = await fetch(`/api/tasks/${task.task_id}/cancel`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      setError(payload?.error?.message ?? "取消监听失败");
      return;
    }
    setTasks((items) => items.map((item) => item.task_id === task.task_id ? payload.task : item));
  }

  return (
    <main className="orders-page">
      <section className="orders-card">
        <span className="eyebrow">Persistent agent tasks</span>
        <h1>补货监听</h1>
        <p className="page-intro">监听只会生成站内通知，不预留库存，也不会自动创建订单。</p>
        {error ? <div className="error-banner" role="alert">{error} · <Link href="/login">前往登录</Link></div> : null}
        <div className="order-list">
          {!error && !tasks.length ? <p className="empty-copy">还没有补货监听。你可以在咨询中为无货书籍创建监听。</p> : null}
          {tasks.map((task) => (
            <article className="notification-row" key={task.task_id}>
              <div>
                <strong>《{task.book_title}》</strong>
                <p>目标数量 {task.desired_quantity} · 最近库存快照 {task.last_seen_available_stock}</p>
                <small>{task.status} · 监听至 {task.expires_at}</small>
              </div>
              <div className="notification-actions">
                <button className="secondary" type="button" disabled={task.status !== "ACTIVE"} onClick={() => void cancel(task)}>
                  {task.status === "CANCELLED" ? "已取消" : "取消监听"}
                </button>
                <Link className="primary link-button" href={`/?buy_book_id=${task.book_id}`}>实时查询商品</Link>
              </div>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
