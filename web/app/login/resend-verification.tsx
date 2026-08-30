"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

export default function ResendVerification() {
  const [busy, setBusy] = useState(false);
  const [until, setUntil] = useState(0);
  const [remaining, setRemaining] = useState(0);
  const [message, setMessage] = useState("邮箱尚未验证。你可以只读咨询；下单和补货监听需验证后重新登录。");
  const [verified, setVerified] = useState(false);
  useEffect(() => {
    const tick = () => setRemaining(Math.max(0, Math.ceil((until - Date.now()) / 1000)));
    tick();
    const timer = setInterval(tick, 250);
    return () => clearInterval(timer);
  }, [until]);

  async function resend() {
    if (busy || Date.now() < until || verified) return;
    setBusy(true);
    try {
      const response = await fetch("/api/auth/resend-verification", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const payload = await response.json();
      const delay = Number(response.status === 429 ? response.headers.get("Retry-After") : payload.retry_after_seconds);
      if (Number.isFinite(delay) && delay > 0) setUntil(Date.now() + Math.ceil(delay) * 1000);
      if (payload.status === "ALREADY_VERIFIED" && response.ok) {
        setVerified(true);
        setMessage("邮箱已经验证，请重新登录以更新登录凭证。");
      } else if (payload.status === "SMTP_ACCEPTED" && response.ok) {
        setMessage("邮件服务器已接收验证邮件，但不保证已到达收件箱。请查看邮件，验证后重新登录。");
      } else if (payload.status === "DELIVERY_UNCONFIRMED" && response.ok) {
        setMessage("无法确认验证邮件发送；邮件仍可能到达，原有效链接仍可使用。请等待冷却后手动重发。");
      } else if (response.status === 429) {
        setMessage("重发次数受到限制，请等待倒计时结束后手动重试。");
      } else if (response.status === 401) {
        setMessage("登录已失效，请重新登录后重发。");
      } else {
        setMessage("未取得重发结果；邮件可能已经发送，请先检查收件箱，稍后手动重试。");
      }
    } catch {
      setMessage("未取得重发结果；邮件可能已经发送，请先检查收件箱，稍后手动重试。");
    } finally { setBusy(false); }
  }

  return <section aria-label="邮箱验证提示">
    <p role="status">{message}</p>
    <button className="primary" type="button" disabled={busy || remaining > 0 || verified} onClick={resend}>
      {busy ? "正在重发…" : remaining > 0 ? `${remaining} 秒后可尝试重发` : "重发验证邮件"}
    </button>
    <p><Link href="/login">重新登录</Link> · <Link href="/">继续只读咨询</Link></p>
  </section>;
}
