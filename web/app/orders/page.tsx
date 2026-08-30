"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Order = { order_id: number; status: string; total_amount: number; currency: string; created_at: string; items: Array<{ title: string }> };

export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    fetch("/api/orders?limit=20", { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.error?.message ?? "订单加载失败");
        setOrders(payload.orders ?? []);
      })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "订单加载失败"));
  }, []);

  return (
    <main className="orders-page">
      <section className="orders-card">
        <span className="eyebrow">Order history</span>
        <h1>我的模拟订单</h1>
        {error ? <div className="error-banner" role="alert">{error} · <Link href="/login">前往登录</Link></div> : null}
        <div className="order-list">
          {!error && !orders.length ? <p className="empty-copy">还没有订单。订单只会在你明确确认预览后创建。</p> : null}
          {orders.map((order) => (
            <article className="order-row" key={order.order_id}>
              <div><strong>订单 #{order.order_id}</strong><small>{order.items.map((item) => item.title).join("、") || "订单商品"}</small></div>
              <div><strong>{(order.total_amount / 100).toFixed(2)} {order.currency}</strong><small>{order.status} · {order.created_at}</small></div>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
