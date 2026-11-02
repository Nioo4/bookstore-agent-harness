import { createContext, FormEvent, KeyboardEvent, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { parseSse, StreamEvent } from "./lib/sse";
import { formatCost, formatDuration, formatRunTokens, formatTokens, formatUserQuestion, TraceDetail, TraceRun } from "./lib/trace";
import "./styles.css";

type Session = { authenticated: boolean; user: { display_name: string; email_verified?: boolean } | null };
const signedOut: Session = { authenticated: false, user: null };
type SessionState = { session: Session | null; setSession: (session: Session) => void };
const SessionContext = createContext<SessionState>({ session: null, setSession: () => undefined });
let sessionRequest: Promise<Session> | undefined;

function loadSession(): Promise<Session> {
  sessionRequest ??= fetch("/api/session", { cache: "no-store", credentials: "same-origin" })
    .then(async (response) => response.ok ? response.json() as Promise<Session> : signedOut)
    .then((value) => value.authenticated ? value : signedOut)
    .catch(() => signedOut);
  return sessionRequest;
}

class ApiError extends Error {
  constructor(public status: number, message: string, public code = "REQUEST_FAILED") { super(message); }
}

async function jsonFetch<T>(input: RequestInfo | URL, init: RequestInit = {}): Promise<T> {
  const response = await fetch(input, { ...init, credentials: "same-origin", cache: "no-store" });
  const payload = await response.json().catch(() => ({})) as { error?: { message?: string; code?: string } } & T;
  if (!response.ok) throw new ApiError(response.status, payload.error?.message ?? "请求未完成", payload.error?.code ?? "REQUEST_FAILED");
  return payload;
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  useEffect(() => {
    let active = true;
    void loadSession().then((value) => { if (active) setSession(value); });
    return () => { active = false; };
  }, []);
  return <SessionContext.Provider value={{ session, setSession }}><BrowserRouter><Navigation /><Routes>
    <Route path="/" element={<Home />} />
    <Route path="/login" element={<LoginPage />} />
    <Route path="/register" element={<RegisterPage />} />
    <Route path="/verify-email" element={<VerifyEmailPage />} />
    <Route path="/orders" element={<OrdersPage />} />
    <Route path="/memories" element={<MemoriesPage />} />
    <Route path="/tasks" element={<TasksPage />} />
    <Route path="/notifications" element={<NotificationsPage />} />
    <Route path="/traces" element={<TracesPage />} />
    <Route path="/traces/:runId" element={<TraceDetailPage />} />
    <Route path="*" element={<Home />} />
  </Routes></BrowserRouter></SessionContext.Provider>;
}

function Navigation() {
  const { session, setSession } = useContext(SessionContext);
  const [menuOpen, setMenuOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const logoutRef = useRef<HTMLButtonElement>(null);

  function focusLogout() { requestAnimationFrame(() => logoutRef.current?.focus()); }
  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Escape") { setMenuOpen(false); return; }
    if (event.key === "ArrowDown") { event.preventDefault(); setMenuOpen(true); focusLogout(); }
  }
  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") { event.preventDefault(); setMenuOpen(false); triggerRef.current?.focus(); }
  }
  async function logout() {
    setLoggingOut(true); setError("");
    try {
      await jsonFetch<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
      setSession(signedOut); setMenuOpen(false); window.location.assign("/");
    } catch { setError("登出未完成，请稍后重试。"); setLoggingOut(false); }
  }

  return <header className="topbar">
    <Link className="brand" to="/" aria-label="页间智能书店首页"><span className="brand-mark" aria-hidden="true">页</span><span><strong>页间</strong><small>智能书店</small></span></Link>
    <nav aria-label="主导航">
      <Link to="/">咨询</Link><Link to="/orders">我的订单</Link><Link to="/tasks">补货监听</Link><Link to="/notifications">通知</Link><Link to="/memories">我的记忆</Link><Link to="/traces">执行轨迹</Link>
      {session === null ? <span className="nav-session-placeholder" aria-label="正在获取登录状态">…</span> : null}
      {session?.authenticated ? <div className="account-menu" onMouseEnter={() => setMenuOpen(true)} onMouseLeave={() => setMenuOpen(false)}>
        <button ref={triggerRef} className="account-trigger" type="button" aria-haspopup="menu" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)} onKeyDown={onTriggerKeyDown}>{session.user?.display_name || "账户"}</button>
        {menuOpen ? <div className="account-popover" role="menu" aria-label="账户菜单" onKeyDown={onMenuKeyDown}><button ref={logoutRef} type="button" role="menuitem" onClick={() => void logout()} disabled={loggingOut}>{loggingOut ? "登出中…" : "登出"}</button></div> : null}
        {error ? <span className="account-error" role="status">{error}</span> : null}
      </div> : session ? <Link to="/login">登录</Link> : null}
    </nav>
  </header>;
}

type Message = { role: "user" | "assistant"; content: string };
type Recommendation = { book_id: number; title?: string; price: number; currency: string; available_stock: number; reason: string; evidence_ids?: string[] };
type Confirmation = { action_type?: "CREATE_ORDER"; confirmation_token: string; expires_at?: string; items: Array<{ title: string; quantity: number; line_total: number }>; total_amount: number; currency: string };
type InventoryWatchConfirmation = { action_type: "CREATE_INVENTORY_WATCH"; conversation_id: string; book: { book_id: number; title: string; available_stock: number }; desired_quantity: number; expires_at: string; submit_path?: "/api/tasks/inventory-watches"; idempotency_key: string };
type InventoryWatchResult = { task: { task_id: string; status: string; book_title: string; desired_quantity: number; expires_at: string }; replayed: boolean };
type ConfirmationResult = { confirmation_id: string; decision: "APPROVE" | "REJECT"; status: "COMPLETED" | "CANCELLED"; order: { order_id: number; status: string; total_amount: number; currency: string } | null; replayed: boolean; trace_id: string };

const examples = ["推荐三本每本不超过100元且有货的分布式系统入门书", "有没有 Alice 写的中文技术书？", "订单确认规则是什么？并注明来源"];
const labels: Record<string, string> = { "run.started": "正在建立安全执行环境", "agent.started": "正在理解你的需求", "tool.started": "正在读取权威业务数据", "tool.completed": "业务数据读取完成", "evidence.retrieved": "知识证据检索完成", "agent.completed": "回答准备完成", "verification.completed": "事实与约束核验完成", "confirmation.required": "订单预览等待确认", "watch_confirmation.required": "补货监听等待确认", "message.completed": "回答已完成" };

function failedRunMessage(code: unknown): string {
  const diagnosticCode = typeof code === "string" && code ? code : "UNKNOWN";
  if (diagnosticCode === "TOOL_BUDGET_EXCEEDED") return "本轮检索已达到安全预算，请缩小问题范围或稍后重试（错误代码：TOOL_BUDGET_EXCEEDED）。";
  if (["BUSINESS_RPC_ERROR", "DEPENDENCY_UNAVAILABLE", "RAG_RESULT_UNAVAILABLE"].includes(diagnosticCode)) return `本次查询服务暂时不可用，请稍后重试（错误代码：${diagnosticCode}）。`;
  return `请求未完成（${diagnosticCode}）`;
}

function Home() {
  const { session } = useContext(SessionContext);
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
    if (bookId && /^\d+$/.test(bookId)) setContent(`我想购买书籍 #${bookId}，请先重新查询实时商品和库存，再生成订单预览供我确认。`);
  }, []);

  async function submit(event?: FormEvent) {
    event?.preventDefault(); const prompt = content.trim(); if (!prompt || running) return;
    setContent(""); setError(""); setEvents([]); setRecommendations([]); setConfirmation(null); setConfirmationResult(null); setWatchConfirmation(null); setWatchResult(null); setStreamingText(""); setMessages((current) => [...current, { role: "user", content: prompt }]); setRunning(true);
    let assistantText = "";
    try {
      const response = await fetch(`/api/chat/conversations/${conversationId}/messages`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: prompt, developer_mode: developerMode }) });
      for await (const streamEvent of parseSse(response)) {
        if (streamEvent.event !== "message.delta") setEvents((current) => [...current, streamEvent]);
        if (streamEvent.event === "message.delta") { assistantText += String(streamEvent.data.content ?? ""); setStreamingText(assistantText); }
        if (streamEvent.event === "confirmation.required" || streamEvent.event === "watch_confirmation.required") {
          if (streamEvent.data.action_type === "CREATE_INVENTORY_WATCH") setWatchConfirmation({ ...(streamEvent.data as unknown as Omit<InventoryWatchConfirmation, "idempotency_key">), idempotency_key: crypto.randomUUID() });
          else setConfirmation(streamEvent.data as unknown as Confirmation);
        }
        if (streamEvent.event === "message.completed") { const finalText = String(streamEvent.data.content ?? assistantText); setMessages((current) => [...current, { role: "assistant", content: finalText }]); setStreamingText(""); setRecommendations((streamEvent.data.recommendations as Recommendation[] | undefined) ?? []); }
        if (streamEvent.event === "run.failed") setError(failedRunMessage(streamEvent.data.code));
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "智能客服暂时不可用"); }
    finally { setRunning(false); setStreamingText(""); }
  }

  async function decide(decision: "APPROVE" | "REJECT") {
    if (!confirmation || deciding) return; setDeciding(true); setError("");
    try { const payload = await jsonFetch<ConfirmationResult>(`/api/chat/conversations/${conversationId}/confirmations`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation_token: confirmation.confirmation_token, decision }) }); setConfirmationResult(payload); setConfirmation(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "确认操作未完成"); }
    finally { setDeciding(false); }
  }

  async function createInventoryWatch() {
    if (!watchConfirmation || deciding) return; setDeciding(true); setError("");
    try { const payload = await jsonFetch<InventoryWatchResult>(watchConfirmation.submit_path ?? "/api/tasks/inventory-watches", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": watchConfirmation.idempotency_key }, body: JSON.stringify({ conversation_id: watchConfirmation.conversation_id, book_id: watchConfirmation.book.book_id, desired_quantity: watchConfirmation.desired_quantity, expires_at: watchConfirmation.expires_at }) }); setWatchResult(payload); setWatchConfirmation(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "补货监听创建失败"); }
    finally { setDeciding(false); }
  }
  function keyboardSubmit(event: KeyboardEvent<HTMLTextAreaElement>) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }

  return <main className="shell"><section className="chat-panel" aria-label="智能书店对话">
    <div className="chat-hero"><span className="eyebrow">Evidence-first bookstore</span><h1>从一本好书开始，得到有据可查的回答。</h1><p>实时查询商品、库存与政策来源。每条推荐都经过约束核验，下单只会在你明确确认后发生。</p><div className="suggestions" aria-label="示例问题">{examples.map((example) => <button className="suggestion" key={example} onClick={() => setContent(example)}>{example}</button>)}</div></div>
    <div className="conversation" aria-live="polite">
      {messages.length === 0 && !running ? <p className="empty-copy">写下你的阅读目标、预算、语言或作者偏好。游客可以咨询；登录并验证邮箱后可以准备订单。</p> : null}
      {messages.map((message, index) => <div className={`message ${message.role}`} key={`${message.role}-${index}`}>{message.content}</div>)}
      {running ? <div className="message assistant">{streamingText || "正在核对信息与证据…"}</div> : null}
      {error ? <div className="error-banner" role="alert">{error}</div> : null}
      {recommendations.length ? <div className="book-grid" aria-label="推荐书籍">{recommendations.map((book) => <article className="book-card" key={book.book_id}><strong>{book.title ?? `书籍 #${book.book_id}`}</strong><div className="book-meta"><span>{(book.price / 100).toFixed(2)} {book.currency}</span><span>库存 {book.available_stock}</span></div><div className="book-reason">{book.reason}</div></article>)}</div> : null}
      {confirmation ? <section className="confirmation" aria-label="订单确认卡片"><h3>订单预览</h3><p>{confirmation.items.length} 项 · {(confirmation.total_amount / 100).toFixed(2)} {confirmation.currency}</p><div className="confirmation-actions"><button type="button" className="secondary" disabled={deciding} onClick={() => void decide("REJECT")}>拒绝</button><button type="button" className="primary" disabled={deciding} onClick={() => void decide("APPROVE")}>{deciding ? "提交中" : "确认并创建模拟订单"}</button></div><small>确认后创建模拟订单并扣减演示库存；不会产生真实支付。</small></section> : null}
      {confirmationResult ? <section className="confirmation result" aria-label="确认结果卡片">{confirmationResult.order ? <><h3>模拟订单已创建</h3><p>订单 #{confirmationResult.order.order_id} · {(confirmationResult.order.total_amount / 100).toFixed(2)} {confirmationResult.order.currency}</p></> : <><h3>已拒绝订单预览</h3><p>未创建订单，也未扣减库存。</p></>}{confirmationResult.replayed ? <small>已返回此前的同一决策结果。</small> : null}</section> : null}
      {watchConfirmation ? <section className="confirmation inventory-watch" aria-label="补货监听确认卡片"><h3>创建补货监听</h3><p>《{watchConfirmation.book.title}》当前库存 {watchConfirmation.book.available_stock}，达到 {watchConfirmation.desired_quantity} 本时通知你。</p><p>监听至 {watchConfirmation.expires_at}</p><div className="confirmation-actions"><button type="button" className="secondary" disabled={deciding} onClick={() => setWatchConfirmation(null)}>取消</button><button type="button" className="primary" disabled={deciding} onClick={() => void createInventoryWatch()}>{deciding ? "提交中" : "确认创建监听"}</button></div><small>只创建站内通知，不会预留库存、创建订单或扣减库存。</small></section> : null}
      {watchResult ? <section className="confirmation result" aria-label="补货监听结果卡片"><h3>补货监听已创建</h3><p>《{watchResult.task.book_title}》达到 {watchResult.task.desired_quantity} 本时会生成站内通知。</p><small>{watchResult.replayed ? "已返回此前创建的同一监听。" : `监听编号 ${watchResult.task.task_id}`}</small></section> : null}
    </div>
    <form className="composer" onSubmit={submit}><div className="composer-row"><textarea aria-label="输入咨询内容" placeholder="例如：推荐两本 80 元以内的英文数据库入门书…" value={content} onChange={(event) => setContent(event.target.value)} onKeyDown={keyboardSubmit} maxLength={8000} rows={2} /><button className="primary" disabled={running || !content.trim()} type="submit">{running ? "处理中" : "发送"}</button></div><div className="composer-meta"><label className="toggle"><input type="checkbox" checked={developerMode} onChange={(event) => setDeveloperMode(event.target.checked)} />开发者模式</label><span>Enter 发送 · Shift + Enter 换行</span></div>{session?.authenticated ? <p className="guest-data-boundary">当前会话已登录：可保留短期上下文并查看应用 Trace；业务事实仍会在每轮重新查询。</p> : session ? <p className="guest-data-boundary">未登录时为单轮临时咨询：本项目不保存对话、应用 Trace 或记忆，但本轮正文仍会发送给当前配置的模型 Provider。</p> : null}</form>
  </section><aside className="side-panel" aria-label="执行进度"><h2>可验证执行</h2><div className="progress-list">{events.length ? events.map((event, index) => <div className="progress-item" key={`${event.event}-${index}`}><span className="progress-dot" aria-hidden="true" /><span>{labels[event.event] ?? event.event}</span></div>) : <p className="empty-copy">模型、工具读取和证据核验状态会显示在这里。</p>}</div>{developerMode && events.length ? <pre className="dev-data">{JSON.stringify(events.at(-1)?.data, null, 2)}</pre> : null}<div className="trust-note">商品事实来自实时业务工具；政策必须附来源；任何写操作都需要认证和显式确认。</div></aside></main>;
}

function LoginPage() {
  const navigate = useNavigate(); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [unverified, setUnverified] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setBusy(true); setError(""); const form = new FormData(event.currentTarget); try { const payload = await jsonFetch<{ user?: { email_verified?: boolean } }>("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: form.get("email"), password: form.get("password") }) }); if (payload.user?.email_verified === false) setUnverified(true); else window.location.assign("/"); } catch (cause) { setError(cause instanceof Error ? cause.message : "登录失败"); } finally { setBusy(false); } }
  return <main className="auth-page"><form className="auth-card" onSubmit={submit}><span className="eyebrow">Welcome back</span><h1>继续你的阅读旅程</h1><p>登录后可以准备订单、查看历史订单、保存和管理长期偏好。下单及创建补货监听前需先验证邮箱。</p><label className="form-field">邮箱<input name="email" type="email" autoComplete="email" required /></label><label className="form-field">密码<input name="password" type="password" autoComplete="current-password" minLength={8} required /></label>{error ? <div className="error-banner" role="alert">{error}</div> : null}<button className="primary" disabled={busy} type="submit">{busy ? "登录中…" : "登录"}</button><div className="form-footer">还没有账号？ <Link to="/register">创建账号</Link></div>{unverified ? <ResendVerification /> : null}</form></main>;
}

function ResendVerification() {
  const [busy, setBusy] = useState(false); const [until, setUntil] = useState(0); const [remaining, setRemaining] = useState(0); const [message, setMessage] = useState("邮箱尚未验证。你可以只读咨询；下单和补货监听需验证后重新登录。"); const [verified, setVerified] = useState(false);
  useEffect(() => { const tick = () => setRemaining(Math.max(0, Math.ceil((until - Date.now()) / 1000))); tick(); const timer = setInterval(tick, 250); return () => clearInterval(timer); }, [until]);
  async function resend() { if (busy || Date.now() < until || verified) return; setBusy(true); try { const response = await fetch("/api/auth/resend-verification", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" }); const payload = await response.json() as { status?: string; retry_after_seconds?: number }; const delay = Number(response.status === 429 ? response.headers.get("Retry-After") : payload.retry_after_seconds); if (Number.isFinite(delay) && delay > 0) setUntil(Date.now() + Math.ceil(delay) * 1000); if (payload.status === "ALREADY_VERIFIED" && response.ok) { setVerified(true); setMessage("邮箱已经验证，请重新登录以更新登录凭证。"); } else if (payload.status === "SMTP_ACCEPTED" && response.ok) setMessage("邮件服务器已接收验证邮件，但不保证已到达收件箱。请查看邮件，验证后重新登录。"); else if (payload.status === "DELIVERY_UNCONFIRMED" && response.ok) setMessage("无法确认验证邮件发送；邮件仍可能到达，原有效链接仍可使用。请等待冷却后手动重发。"); else if (response.status === 429) setMessage("重发次数受到限制，请等待倒计时结束后手动重试。"); else if (response.status === 401) setMessage("登录已失效，请重新登录后重发。"); else setMessage("未取得重发结果；邮件可能已经发送，请先检查收件箱，稍后手动重试。"); } catch { setMessage("未取得重发结果；邮件可能已经发送，请先检查收件箱，稍后手动重试。"); } finally { setBusy(false); } }
  return <section aria-label="邮箱验证提示"><p role="status">{message}</p><button className="primary" type="button" disabled={busy || remaining > 0 || verified} onClick={() => void resend()}>{busy ? "正在重发…" : remaining > 0 ? `${remaining} 秒后可尝试重发` : "重发验证邮件"}</button><p><Link to="/login">重新登录</Link> · <Link to="/">继续只读咨询</Link></p></section>;
}

function RegisterPage() {
  const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [created, setCreated] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setBusy(true); setError(""); setMessage(""); const form = new FormData(event.currentTarget); try { const payload = await jsonFetch<{ verification_email_sent?: boolean }>("/api/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: form.get("email"), password: form.get("password"), display_name: form.get("display_name") }) }); setCreated(true); if (payload.verification_email_sent === true) setMessage("邮件服务器已接收验证邮件。请打开邮件中的链接完成验证后再登录；未收到时可登录后重发。"); else setError("账号已创建，但无法确认验证邮件发送；请登录后重发，请勿重复注册。"); } catch { setError("未取得注册结果，请先确认账号状态后再重试。"); } finally { setBusy(false); } }
  return <main className="auth-page"><form className="auth-card" onSubmit={submit}><span className="eyebrow">Create account</span><h1>建立你的书架身份</h1><p>注册后请先验证邮箱。未验证账号可以咨询，但不能准备订单或创建补货监听。</p><label className="form-field">显示名称<input name="display_name" maxLength={80} required /></label><label className="form-field">邮箱<input name="email" type="email" autoComplete="email" required /></label><label className="form-field">密码<input name="password" type="password" autoComplete="new-password" minLength={8} maxLength={128} required /></label>{error ? <div className="error-banner" role="alert">{error}</div> : null}{message ? <div className="message assistant" role="status">{message}</div> : null}{created ? <Link to="/login">登录后重发验证邮件</Link> : null}<button className="primary" disabled={busy || created} type="submit">{busy ? "提交中…" : "注册并发送验证邮件"}</button><div className="form-footer">已有账号？ <Link to="/login">去登录</Link></div></form></main>;
}

let currentVerificationAttempt: Promise<{ ok: boolean; message: string }> | undefined;
function consumeVerificationLink() {
  const url = new URL(window.location.href); const token = url.searchParams.get("token"); if (url.search) window.history.replaceState(null, "", url.pathname);
  if (token) currentVerificationAttempt = fetch("/api/auth/verify-email", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }).then(async (response) => { const payload = await response.json() as { error?: { code?: string } }; if (response.ok) return { ok: true, message: "邮箱验证成功，请重新登录更新凭证后使用订单和补货监听功能。" }; const messages: Record<string, string> = { EMAIL_VERIFICATION_INVALID: "验证链接无效，请登录后重发。", EMAIL_VERIFICATION_EXPIRED: "验证链接已过期，请登录后重发。", EMAIL_VERIFICATION_ALREADY_USED: "链接已使用或邮箱已验证，请重新登录；仍未验证时可登录后重发。" }; return { ok: false, message: messages[payload.error?.code ?? ""] ?? "暂未取得验证结果，请重新登录确认邮箱状态。" }; }).catch(() => ({ ok: false, message: "暂未取得验证结果，请重新登录确认邮箱状态。" }));
  return currentVerificationAttempt ?? Promise.resolve({ ok: false, message: "验证链接缺少令牌，请登录后重发。" });
}
function VerifyEmailPage() { const [result, setResult] = useState({ ok: false, message: "正在验证邮箱…" }); useEffect(() => { let active = true; void consumeVerificationLink().then((value) => { if (active) setResult(value); }); return () => { active = false; }; }, []); return <main className="auth-page"><section className="auth-card"><span className="eyebrow">Email verification</span><h1>{result.ok ? "验证完成" : "验证邮箱"}</h1><p role="status">{result.message}</p><Link className="primary" to="/login">{result.ok ? "重新登录" : "登录后重发 / 确认验证状态"}</Link></section></main>; }

function PageCard({ eyebrow, title, children }: { eyebrow: string; title: string; children: React.ReactNode }) { return <main className="orders-page"><section className="orders-card"><span className="eyebrow">{eyebrow}</span><h1>{title}</h1>{children}</section></main>; }

type Order = { order_id: number; status: string; total_amount: number; currency: string; created_at: string; items: Array<{ title: string }> };
function OrdersPage() { const [orders, setOrders] = useState<Order[]>([]); const [error, setError] = useState(""); useEffect(() => { void jsonFetch<{ orders?: Order[] }>("/api/orders?limit=20").then((payload) => setOrders(payload.orders ?? [])).catch((cause) => setError(cause instanceof Error ? cause.message : "订单加载失败")); }, []); return <PageCard eyebrow="Order history" title="我的模拟订单">{error ? <div className="error-banner" role="alert">{error} · <Link to="/login">前往登录</Link></div> : null}<div className="order-list">{!error && !orders.length ? <p className="empty-copy">还没有订单。订单只会在你明确确认预览后创建。</p> : null}{orders.map((order) => <article className="order-row" key={order.order_id}><div><strong>订单 #{order.order_id}</strong><small>{order.items?.map((item) => item.title).join("、") || "订单商品"}</small></div><div><strong>{(order.total_amount / 100).toFixed(2)} {order.currency}</strong><small>{order.status} · {order.created_at}</small></div></article>)}</div></PageCard>; }

type MemoryValue = { value?: string; items?: string[]; currency?: string; max_amount?: number; summary?: string };
type Memory = { memory_id: string; memory_key?: string; key?: string; memory_type?: "PREFERENCE" | "NEGATIVE_PREFERENCE" | "EPISODE"; value: MemoryValue | string; source?: "EXPLICIT" | "INFERRED"; updated_at?: string };
const memoryNames: Record<string, string> = { "preference.languages": "偏好语言", "preference.topics": "偏好主题", "preference.authors": "偏好作者", PREFERRED_LANGUAGE: "偏好语言", PREFERRED_TOPIC: "偏好主题", PREFERRED_AUTHOR: "偏好作者", MAX_USUAL_BUDGET: "常用预算", EXCLUDED_TOPIC: "排除主题", EXCLUDED_AUTHOR: "排除作者" };
function memoryDescription(memory: Memory) {
  const scalar = typeof memory.value === "string" ? memory.value : memory.value.value;
  if (typeof scalar === "string") {
    if ((memory.memory_key ?? memory.key) === "PREFERRED_LANGUAGE") return ({ "zh-CN": "中文（zh-CN）", "zh-TW": "繁体中文（zh-TW）", en: "英文（en）" } as Record<string, string>)[scalar] ?? scalar;
    if ((memory.memory_key ?? memory.key) === "MAX_USUAL_BUDGET" && /^\d+$/.test(scalar)) return `${(Number(scalar) / 100).toFixed(2)} CNY`;
    return scalar;
  }
  const structured = memory.value as MemoryValue;
  if (structured.items) return structured.items.join("、");
  if (typeof structured.max_amount === "number") return `${(structured.max_amount / 100).toFixed(2)} ${structured.currency ?? ""}`;
  return structured.summary ?? "";
}
function MemoriesPage() { const [memories, setMemories] = useState<Memory[]>([]); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState(""); const [key, setKey] = useState("PREFERRED_LANGUAGE"); const [value, setValue] = useState("");
  const load = useCallback(() => { setLoading(true); void jsonFetch<{ memories?: Memory[] }>("/api/memories?limit=100").then((payload) => setMemories(payload.memories ?? [])).catch(() => setError("记忆加载失败，请确认已登录后重试。")).finally(() => setLoading(false)); }, []); useEffect(() => { void load(); }, [load]);
  async function savePreference(event: FormEvent) { event.preventDefault(); if (!value.trim()) return; setBusy(true); setError(""); setMessage(""); try { await jsonFetch("/api/memories/preferences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, value: value.trim() }) }); setValue(""); setMessage("已保存明确偏好。后续咨询会参考它，但本轮明确要求优先。"); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : "偏好保存失败"); } finally { setBusy(false); } }
  async function remove(memory?: Memory) { if (!window.confirm(memory ? "删除这条记忆？删除后不再用于后续推荐。" : "删除全部记忆？此操作不能撤销。")) return; setBusy(true); setError(""); setMessage(""); try { await jsonFetch(memory ? `/api/memories/${encodeURIComponent(memory.memory_id)}` : "/api/memories", { method: "DELETE" }); setMemories((items) => memory ? items.filter((item) => item.memory_id !== memory.memory_id) : []); setMessage(memory ? "已删除这条记忆。" : "已删除全部记忆。"); } catch { setError("删除未成功，请重试。页面未移除原记录。"); } finally { setBusy(false); } }
  return <PageCard eyebrow="Your preferences" title="我的记忆"><p className="page-intro">只保存你明确要求记住的结构化偏好。删除后不再用于后续推荐；临时预算不会自动写入长期记忆。</p><form className="trace-filters" onSubmit={savePreference}><label>偏好类型<select aria-label="偏好类型" value={key} onChange={(event) => setKey(event.target.value)}><option value="PREFERRED_LANGUAGE">语言</option><option value="PREFERRED_TOPIC">主题</option><option value="PREFERRED_AUTHOR">作者</option><option value="MAX_USUAL_BUDGET">常用预算</option><option value="EXCLUDED_TOPIC">排除主题</option><option value="EXCLUDED_AUTHOR">排除作者</option></select></label><label>偏好内容<input aria-label="偏好内容" value={value} onChange={(event) => setValue(event.target.value)} maxLength={255} required /></label><button className="secondary" disabled={busy || !value.trim()}>保存明确偏好</button></form>{error ? <div className="error-banner" role="alert">{error} <Link to="/login">前往登录</Link></div> : null}{message ? <p role="status">{message}</p> : null}<button className="secondary" type="button" disabled={loading || busy || !memories.length} onClick={() => void remove()}>删除全部记忆</button><div className="order-list" aria-busy={loading || busy}>{loading ? <p>正在加载记忆…</p> : !error && !memories.length ? <p className="empty-copy">暂无已保存的记忆。</p> : null}{memories.map((memory) => <article className="notification-row" key={memory.memory_id}><div><strong>{memoryNames[memory.memory_key ?? memory.key ?? ""] ?? (memory.memory_type === "EPISODE" ? "阅读咨询摘要" : "偏好")}</strong><p>{memoryDescription(memory)}</p><small>{memory.source === "EXPLICIT" ? "你明确表达的偏好" : "历史记录（不会自动注入新咨询）"}</small></div><button className="secondary" type="button" disabled={busy} onClick={() => void remove(memory)}>删除这条记忆</button></article>)}</div></PageCard>; }

type Task = { task_id: string; status: "ACTIVE" | "TRIGGERED" | "COMPLETED" | "EXPIRED" | "CANCELLED"; book_id: number; book_title: string; desired_quantity: number; last_seen_available_stock: number; expires_at: string; created_at: string };
function TasksPage() { const [tasks, setTasks] = useState<Task[]>([]); const [error, setError] = useState(""); useEffect(() => { void jsonFetch<{ tasks?: Task[] }>("/api/tasks?type=INVENTORY_WATCH&limit=20").then((payload) => setTasks(payload.tasks ?? [])).catch((cause) => setError(cause instanceof Error ? cause.message : "监听任务加载失败")); }, []); async function cancel(task: Task) { try { const payload = await jsonFetch<{ task: Task }>(`/api/tasks/${task.task_id}/cancel`, { method: "POST" }); setTasks((items) => items.map((item) => item.task_id === task.task_id ? payload.task : item)); } catch (cause) { setError(cause instanceof Error ? cause.message : "取消监听失败"); } } return <PageCard eyebrow="Persistent tasks" title="补货监听"><p className="page-intro">监听只会生成站内通知，不预留库存，也不会自动创建订单。</p>{error ? <div className="error-banner" role="alert">{error} · <Link to="/login">前往登录</Link></div> : null}<div className="order-list">{!error && !tasks.length ? <p className="empty-copy">还没有补货监听。你可以在咨询中为无货书籍创建监听。</p> : null}{tasks.map((task) => <article className="notification-row" key={task.task_id}><div><strong>《{task.book_title}》</strong><p>目标数量 {task.desired_quantity} · 最近库存快照 {task.last_seen_available_stock}</p><small>{task.status} · 监听至 {task.expires_at}</small></div><div className="notification-actions"><button className="secondary" type="button" disabled={task.status !== "ACTIVE"} onClick={() => void cancel(task)}>{task.status === "CANCELLED" ? "已取消" : "取消监听"}</button><Link className="primary link-button" to={`/?buy_book_id=${task.book_id}`}>实时查询商品</Link></div></article>)}</div></PageCard>; }

type Notification = { notification_id: string; status: "UNREAD" | "READ"; title: string; body: string; task_id: string; payload: { book_id: number; book_title_snapshot: string; available_stock: number; desired_quantity: number }; created_at: string };
function NotificationsPage() { const [notifications, setNotifications] = useState<Notification[]>([]); const [error, setError] = useState(""); useEffect(() => { void jsonFetch<{ notifications?: Notification[] }>("/api/notifications?limit=20").then((payload) => setNotifications(payload.notifications ?? [])).catch((cause) => setError(cause instanceof Error ? cause.message : "通知加载失败")); }, []); async function markRead(notification: Notification) { if (notification.status === "READ") return; try { const payload = await jsonFetch<{ notification: Notification }>(`/api/notifications/${notification.notification_id}/read`, { method: "POST" }); setNotifications((items) => items.map((item) => item.notification_id === notification.notification_id ? payload.notification : item)); } catch (cause) { setError(cause instanceof Error ? cause.message : "通知状态更新失败"); } } return <PageCard eyebrow="Inventory snapshots" title="站内通知"><p className="page-intro">库存数值是触发时快照，不是持续承诺。购买前会重新查询实时商品和库存。</p>{error ? <div className="error-banner" role="alert">{error} · <Link to="/login">前往登录</Link></div> : null}<div className="order-list">{!error && !notifications.length ? <p className="empty-copy">暂时没有补货通知。</p> : null}{notifications.map((notification) => <article className={`notification-row ${notification.status === "UNREAD" ? "unread" : ""}`} key={notification.notification_id}><div><strong>{notification.title}</strong><p>{notification.body}</p><small>触发时库存 {notification.payload.available_stock} · {notification.created_at}</small></div><div className="notification-actions"><button className="secondary" type="button" disabled={notification.status === "READ"} onClick={() => void markRead(notification)}>{notification.status === "READ" ? "已读" : "标为已读"}</button><Link className="primary link-button" to={`/?buy_book_id=${notification.payload.book_id}`}>重新查询并购买</Link></div></article>)}</div></PageCard>; }

function TracesPage() {
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    const query = new URLSearchParams({ limit: "50" });
    if (kind) query.set("run_kind", kind);
    if (status) query.set("status", status);
    void jsonFetch<{ runs?: TraceRun[] }>(`/api/traces?${query}`)
      .then((payload) => setRuns((payload.runs ?? []).filter((run) => (run.run_kind ?? "LIVE") === "LIVE")))
      .catch((cause) => setError(cause instanceof Error ? cause.message : "轨迹加载失败"))
      .finally(() => setLoading(false));
  }, [kind, status]);
  useEffect(() => { void load(); }, [load]);
  return <main className="trace-page"><section className="trace-card"><span className="eyebrow">Safe application trace</span><h1>执行轨迹</h1><p className="page-intro">查看模型、工具、检索证据和确定性校验的顺序时间线；不展示隐藏推理。</p><div className="trace-filters"><label>运行类型<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="">全部在线运行</option><option value="LIVE">LIVE</option></select></label><label>状态<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option>{["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"].map((value) => <option key={value}>{value}</option>)}</select></label><button className="secondary" type="button" onClick={() => void load()}>刷新</button></div>{error ? <div className="error-banner" role="alert">{error} · <Link to="/login">前往登录</Link></div> : null}{loading ? <p className="empty-copy">正在加载轨迹…</p> : null}{!loading && !error && !runs.length ? <p className="empty-copy">还没有可查看的执行轨迹。</p> : null}<div className="trace-list">{runs.map((run) => <Link className="trace-row" to={`/traces/${run.run_id}`} key={run.run_id}><div className="trace-row-main"><span className={`status-pill status-${run.status.toLowerCase()}`}>{run.status}</span><strong>LIVE · {run.workflow_type}</strong><p className="trace-question-preview">用户问题：{formatUserQuestion(run)}</p><small>{new Date(run.created_at).toLocaleString("zh-CN")} · {run.run_id}</small></div><div className="trace-row-metrics"><span>总耗时 {formatDuration(run.wall_duration_ms)}</span><span>Token {formatRunTokens(run)}</span><span>成本 {formatCost(run.cost)}</span><span className={run.trace_status === "INCOMPLETE" ? "danger-text" : "muted-text"}>{run.trace_status}</span></div></Link>)}</div></section></main>;
}

function TraceDetailPage() { const { runId = "" } = useParams(); const [detail, setDetail] = useState<TraceDetail | null>(null); const [developerMode, setDeveloperMode] = useState(false); const [error, setError] = useState(""); const load = useCallback(async () => { setError(""); try { setDetail(await jsonFetch<TraceDetail>(`/api/traces/${encodeURIComponent(runId)}?step_limit=200`)); } catch (cause) { setError(cause instanceof Error ? cause.message : "轨迹详情加载失败"); } }, [runId]); useEffect(() => { void load(); }, [load]); async function deleteTrace() { if (!window.confirm("删除后轨迹会立即隐藏，并进入延迟清理。确定继续吗？")) return; try { await jsonFetch(`/api/traces/${encodeURIComponent(runId)}`, { method: "DELETE" }); window.location.assign("/traces"); } catch (cause) { setError(cause instanceof Error ? cause.message : "删除失败"); } }
  if (error && !detail) return <main className="trace-page"><div className="error-banner" role="alert">{error} · <Link to="/traces">返回列表</Link></div></main>; if (!detail) return <main className="trace-page"><p className="empty-copy">正在加载轨迹详情…</p></main>; const run = detail.run;
  return <main className="trace-page"><section className="trace-card trace-detail-card"><div className="trace-title-row"><div><span className="eyebrow">Safe application trace</span><h1>轨迹详情</h1><p className="trace-id">{run.run_id}</p></div><div className="trace-actions"><label className="toggle"><input aria-label="开发者模式" type="checkbox" checked={developerMode} onChange={(event) => setDeveloperMode(event.target.checked)} />开发者模式</label><button className="secondary" type="button" onClick={() => void deleteTrace()}>删除轨迹</button></div></div>{error ? <div className="error-banner" role="alert">{error}</div> : null}<div className="metric-grid"><Metric label="运行状态" value={`${run.status} / ${run.trace_status}`} /><Metric label="总耗时" value={formatDuration(run.wall_duration_ms)} /><Metric label="活动耗时" value={formatDuration(run.active_duration_ms)} /><Metric label="成本" value={formatCost(run.cost)} /></div>{run.error ? <div className="error-banner">{run.error.code}: {run.error.message}</div> : null}<section className="trace-section" aria-labelledby="user-question-title"><h2 id="user-question-title">用户问题</h2><p className="trace-question">{formatUserQuestion(run)}</p><small className="muted-text">仅展示已保存的脱敏内容，不包含隐藏提示词。</small></section><section className="trace-section"><h2>最终回答</h2><p className="final-answer">{run.final_answer ?? "该 Run 尚未生成最终回答。"}</p></section><section className="trace-section"><h2>执行时间线</h2><p>{detail.steps.length} 个步骤 · {detail.evidence.length} 条 Evidence</p></section>{developerMode ? <DeveloperTrace detail={detail} /> : null}</section></main>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong></div>; }
function DeveloperTrace({ detail }: { detail: TraceDetail }) { const run = detail.run; return <section className="trace-section developer-trace"><h2>开发者安全视图</h2><div className="metric-grid compact"><Metric label="模型 Profile" value={run.model_profile} /><Metric label="Prompt 版本" value={run.prompt_set_version} /><Metric label="工具版本" value={run.tool_registry_version.slice(0, 12)} /><Metric label="重试" value={String(run.retry_count)} /></div><p>Token：{formatRunTokens(run)}；prompt {run.token_usage.prompt ?? "未知"} / completion {run.token_usage.completion ?? "未知"} / cached {run.token_usage.cached ?? "—"} / reasoning {run.token_usage.reasoning ?? "—"}</p><div className="timeline">{detail.steps.map((step) => <article className="timeline-step" key={step.step_id}><div><span className="sequence">#{step.sequence_no}</span><strong>{step.step_type} · {step.agent_name ?? step.tool_name ?? step.node_id ?? "system"}</strong><small>{step.status} · {formatDuration(step.duration_ms)} · attempt {step.attempt_no}</small></div><p>本步骤直接调用 Token：{formatTokens(step)}</p><details><summary>安全投影与 Evidence</summary><pre>{JSON.stringify({ input: step.input, output: step.output, evidence_ids: step.evidence_ids, error: step.error }, null, 2)}</pre></details></article>)}</div>{detail.evidence.length ? <div className="evidence-view"><h3>Evidence 安全投影</h3><pre>{JSON.stringify(detail.evidence, null, 2)}</pre></div> : null}</section>; }

export default App;
