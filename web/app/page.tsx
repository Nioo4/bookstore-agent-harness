"use client";

import { FormEvent, KeyboardEvent, useEffect, useMemo, useState } from "react";
import { parseSse, StreamEvent } from "@/lib/sse";

type Message = { role: "user" | "assistant"; content: string };
type Recommendation = {
  book_id: number;
  price: number;
  currency: string;
  available_stock: number;
  reason: string;
};
type Confirmation = {
  action_type?: "CREATE_ORDER";
  confirmation_token: string;
  expires_at?: string;
  items: Array<{ title: string; quantity: number; line_total: number }>;
  total_amount: number;
  currency: string;
};
type InventoryWatchConfirmation = {
  action_type: "CREATE_INVENTORY_WATCH";
  conversation_id: string;
  book: { book_id: number; title: string; available_stock: number };
  desired_quantity: number;
  expires_at: string;
  submit_path: "/api/tasks/inventory-watches";
  idempotency_key: string;
};
type InventoryWatchResult = {
  task: { task_id: string; status: string; book_title: string; desired_quantity: number; expires_at: string };
  replayed: boolean;
};
type ConfirmationResult = {
  confirmation_id: string;
  decision: "APPROVE" | "REJECT";
  status: "COMPLETED" | "CANCELLED";
  order: { order_id: number; status: string; total_amount: number; currency: string } | null;
  replayed: boolean;
  trace_id: string;
};

const examples = [
  "推荐三本每本不超过100元且有货的分布式系统入门书",
  "有没有 Alice 写的中文技术书？",
  "介绍一下配送政策，并注明来源",
];

const labels: Record<string, string> = {
  "run.started": "正在建立安全执行环境",
  "plan.created": "已生成并校验任务计划",
  "agent.started": "专业 Agent 正在处理",
  "tool.started": "正在读取权威业务数据",
  "tool.completed": "业务数据读取完成",
  "agent.completed": "专业 Agent 已完成",
  "verification.completed": "事实与约束核验完成",
  "confirmation.required": "订单预览等待确认",
  "message.completed": "回答已完成",
};

function failedRunMessage(code: unknown): string {
  const diagnosticCode = typeof code === "string" && code ? code : "UNKNOWN";
  if (diagnosticCode === "BUSINESS_RPC_ERROR") {
    return `本次查询服务暂时不可用，请稍后重试（错误代码：${diagnosticCode}）。`;
  }
  return `请求未完成（${diagnosticCode}）`;
}

export default function Home() {
  const [content, setContent] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [confirmationResult, setConfirmationResult] = useState<ConfirmationResult | null>(null);
  const [watchConfirmation, setWatchConfirmation] = useState<InventoryWatchConfirmation | null>(null);
  const [watchResult, setWatchResult] = useState<InventoryWatchResult | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [developerMode, setDeveloperMode] = useState(false);
  const [running, setRunning] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [error, setError] = useState("");
  const conversationId = useMemo(() => crypto.randomUUID(), []);

  useEffect(() => {
    const bookId = new URLSearchParams(window.location.search).get("buy_book_id");
    if (bookId && /^\d+$/.test(bookId)) {
      setContent(`我想购买书籍 #${bookId}，请先重新查询实时商品和库存，再生成订单预览供我确认。`);
    }
  }, []);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const prompt = content.trim();
    if (!prompt || running) return;
    setContent("");
    setError("");
    setEvents([]);
    setRecommendations([]);
    setConfirmation(null);
    setConfirmationResult(null);
    setWatchConfirmation(null);
    setWatchResult(null);
    setStreamingText("");
    setMessages((current) => [...current, { role: "user", content: prompt }]);
    setRunning(true);
    let assistantText = "";
    try {
      const response = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: prompt, developer_mode: developerMode }),
      });
      for await (const streamEvent of parseSse(response)) {
        if (streamEvent.event !== "message.delta") {
          setEvents((current) => [...current, streamEvent]);
        }
        if (streamEvent.event === "message.delta") {
          assistantText += String(streamEvent.data.content ?? "");
          setStreamingText(assistantText);
        }
        if (streamEvent.event === "confirmation.required") {
          if (streamEvent.data.action_type === "CREATE_INVENTORY_WATCH") {
            setWatchConfirmation({
              ...(streamEvent.data as unknown as Omit<InventoryWatchConfirmation, "idempotency_key">),
              idempotency_key: crypto.randomUUID(),
            });
          } else {
            setConfirmation(streamEvent.data as unknown as Confirmation);
          }
        }
        if (streamEvent.event === "message.completed") {
          const finalText = String(streamEvent.data.content ?? assistantText);
          setMessages((current) => [...current, { role: "assistant", content: finalText }]);
          setStreamingText("");
          setRecommendations((streamEvent.data.recommendations as Recommendation[] | undefined) ?? []);
        }
        if (streamEvent.event === "run.failed") {
          setError(failedRunMessage(streamEvent.data.code));
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "智能客服暂时不可用");
    } finally {
      setRunning(false);
      setStreamingText("");
    }
  }

  async function decide(decision: "APPROVE" | "REJECT") {
    if (!confirmation || deciding) return;
    setDeciding(true);
    setError("");
    try {
      const response = await fetch(`/api/chat/conversations/${conversationId}/confirmations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmation_token: confirmation.confirmation_token, decision }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message ?? "确认操作未完成");
      setConfirmationResult(payload as ConfirmationResult);
      setConfirmation(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "确认操作未完成");
    } finally {
      setDeciding(false);
    }
  }

  async function createInventoryWatch() {
    if (!watchConfirmation || deciding) return;
    setDeciding(true);
    setError("");
    try {
      const response = await fetch(watchConfirmation.submit_path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": watchConfirmation.idempotency_key },
        body: JSON.stringify({
          conversation_id: watchConfirmation.conversation_id,
          book_id: watchConfirmation.book.book_id,
          desired_quantity: watchConfirmation.desired_quantity,
          expires_at: watchConfirmation.expires_at,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message ?? "补货监听创建失败");
      setWatchResult(payload as InventoryWatchResult);
      setWatchConfirmation(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "补货监听创建失败");
    } finally {
      setDeciding(false);
    }
  }

  function keyboardSubmit(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <main className="shell">
      <section className="chat-panel" aria-label="智能书店对话">
        <div className="chat-hero">
          <span className="eyebrow">Evidence-first bookstore</span>
          <h1>从一本好书开始，得到有据可查的回答。</h1>
          <p>实时查询商品、库存与政策来源。每条推荐都经过约束核验，下单只会在你明确确认后发生。</p>
          <div className="suggestions" aria-label="示例问题">
            {examples.map((example) => (
              <button className="suggestion" key={example} onClick={() => setContent(example)}>{example}</button>
            ))}
          </div>
        </div>

        <div className="conversation" aria-live="polite">
          {messages.length === 0 && !running ? (
            <p className="empty-copy">写下你的阅读目标、预算、语言或作者偏好。游客可以咨询；登录并验证邮箱后可以准备订单。</p>
          ) : null}
          {messages.map((message, index) => (
            <div className={`message ${message.role}`} key={`${message.role}-${index}`}>{message.content}</div>
          ))}
          {running ? <div className="message assistant">{streamingText || "正在核对信息与证据…"}</div> : null}
          {error ? <div className="error-banner" role="alert">{error}</div> : null}

          {recommendations.length ? (
            <div className="book-grid" aria-label="推荐书籍">
              {recommendations.map((book) => (
                <article className="book-card" key={book.book_id}>
                  <strong>书籍 #{book.book_id}</strong>
                  <div className="book-meta">
                    <span>{(book.price / 100).toFixed(2)} {book.currency}</span>
                    <span>库存 {book.available_stock}</span>
                  </div>
                  <div className="book-reason">{book.reason}</div>
                </article>
              ))}
            </div>
          ) : null}

          {confirmation ? (
            <section className="confirmation" aria-label="订单确认卡片">
              <h3>订单预览</h3>
              <p>{confirmation.items.length} 项 · {(confirmation.total_amount / 100).toFixed(2)} {confirmation.currency}</p>
              <div className="confirmation-actions">
                <button type="button" className="secondary" disabled={deciding} onClick={() => void decide("REJECT")}>拒绝</button>
                <button type="button" className="primary" disabled={deciding} onClick={() => void decide("APPROVE")}>{deciding ? "提交中" : "确认并创建模拟订单"}</button>
              </div>
              <small>确认后创建模拟订单并扣减演示库存；不会产生真实支付。</small>
            </section>
          ) : null}

          {confirmationResult ? (
            <section className="confirmation result" aria-label="确认结果卡片">
              {confirmationResult.order ? (
                <>
                  <h3>模拟订单已创建</h3>
                  <p>订单 #{confirmationResult.order.order_id} · {(confirmationResult.order.total_amount / 100).toFixed(2)} {confirmationResult.order.currency}</p>
                </>
              ) : (
                <>
                  <h3>已拒绝订单预览</h3>
                  <p>未创建订单，也未扣减库存。</p>
                </>
              )}
              {confirmationResult.replayed ? <small>已返回此前的同一决策结果。</small> : null}
            </section>
          ) : null}

          {watchConfirmation ? (
            <section className="confirmation inventory-watch" aria-label="补货监听确认卡片">
              <h3>创建补货监听</h3>
              <p>《{watchConfirmation.book.title}》当前库存 {watchConfirmation.book.available_stock}，达到 {watchConfirmation.desired_quantity} 本时通知你。</p>
              <p>监听至 {watchConfirmation.expires_at}</p>
              <div className="confirmation-actions">
                <button type="button" className="secondary" disabled={deciding} onClick={() => setWatchConfirmation(null)}>取消</button>
                <button type="button" className="primary" disabled={deciding} onClick={() => void createInventoryWatch()}>{deciding ? "提交中" : "确认创建监听"}</button>
              </div>
              <small>只创建站内通知，不会预留库存、创建订单或扣减库存。</small>
            </section>
          ) : null}

          {watchResult ? (
            <section className="confirmation result" aria-label="补货监听结果卡片">
              <h3>补货监听已创建</h3>
              <p>《{watchResult.task.book_title}》达到 {watchResult.task.desired_quantity} 本时会生成站内通知。</p>
              <small>{watchResult.replayed ? "已返回此前创建的同一监听。" : `监听编号 ${watchResult.task.task_id}`}</small>
            </section>
          ) : null}
        </div>

        <form className="composer" onSubmit={submit}>
          <div className="composer-row">
            <textarea
              aria-label="输入咨询内容"
              placeholder="例如：推荐两本 80 元以内的英文数据库入门书…"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              onKeyDown={keyboardSubmit}
              maxLength={8000}
              rows={2}
            />
            <button className="primary" disabled={running || !content.trim()} type="submit">{running ? "处理中" : "发送"}</button>
          </div>
          <div className="composer-meta">
            <label className="toggle"><input type="checkbox" checked={developerMode} onChange={(event) => setDeveloperMode(event.target.checked)} />开发者模式</label>
            <span>Enter 发送 · Shift + Enter 换行</span>
          </div>
          <p className="guest-data-boundary">
            未登录时为单轮临时咨询：本项目不保存对话、应用 Trace 或记忆，但本轮正文仍会发送给当前配置的模型 Provider。
          </p>
        </form>
      </section>

      <aside className="side-panel" aria-label="执行进度">
        <h2>可验证执行</h2>
        <div className="progress-list">
          {events.length ? events.map((event, index) => (
            <div className="progress-item" key={`${event.event}-${index}`}>
              <span className="progress-dot" aria-hidden="true" />
              <span>{labels[event.event] ?? event.event}</span>
            </div>
          )) : <p className="empty-copy">Agent 的计划、工具读取和核验状态会显示在这里。</p>}
        </div>
        {developerMode && events.length ? <pre className="dev-data">{JSON.stringify(events.at(-1)?.data, null, 2)}</pre> : null}
        <div className="trust-note">商品事实来自实时业务工具；政策必须附来源；任何写操作都需要认证和显式确认。</div>
      </aside>
    </main>
  );
}
