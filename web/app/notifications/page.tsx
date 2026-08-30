"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Notification = {
  notification_id: string;
  type: "INVENTORY_AVAILABLE";
  status: "UNREAD" | "READ";
  title: string;
  body: string;
  task_id: string;
  payload: { book_id: number; book_title_snapshot: string; available_stock: number; desired_quantity: number };
  created_at: string;
  read_at: string | null;
};

export default function NotificationsPage() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/notifications?limit=20", { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.error?.message ?? "通知加载失败");
        setNotifications(payload.notifications ?? []);
      })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "通知加载失败"));
  }, []);

  async function markRead(notification: Notification) {
    if (notification.status === "READ") return;
    const response = await fetch(`/api/notifications/${notification.notification_id}/read`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      setError(payload?.error?.message ?? "通知状态更新失败");
      return;
    }
    setNotifications((items) => items.map((item) => item.notification_id === notification.notification_id ? payload.notification : item));
  }

  return (
    <main className="orders-page">
      <section className="orders-card">
        <span className="eyebrow">Inventory snapshots</span>
        <h1>站内通知</h1>
        <p className="page-intro">库存数值是触发时快照，不是持续承诺。购买前会重新查询实时商品和库存。</p>
        {error ? <div className="error-banner" role="alert">{error} · <Link href="/login">前往登录</Link></div> : null}
        <div className="order-list">
          {!error && !notifications.length ? <p className="empty-copy">暂时没有补货通知。</p> : null}
          {notifications.map((notification) => (
            <article className={`notification-row ${notification.status === "UNREAD" ? "unread" : ""}`} key={notification.notification_id}>
              <div>
                <strong>{notification.title}</strong>
                <p>{notification.body}</p>
                <small>触发时库存 {notification.payload.available_stock} · {notification.created_at}</small>
              </div>
              <div className="notification-actions">
                <button className="secondary" type="button" disabled={notification.status === "READ"} onClick={() => void markRead(notification)}>
                  {notification.status === "READ" ? "已读" : "标为已读"}
                </button>
                <Link className="primary link-button" href={`/?buy_book_id=${notification.payload.book_id}`}>重新查询并购买</Link>
              </div>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
