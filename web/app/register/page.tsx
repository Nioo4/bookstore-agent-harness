"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

export default function RegisterPage() {
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password: form.get("password"), display_name: form.get("display_name") }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setError(payload?.error?.message ?? "注册失败");
      } else {
        setCreated(true);
        if (payload.verification_email_sent === true) {
          setMessage("邮件服务器已接收验证邮件。请打开邮件中的链接完成验证后再登录；未收到时可登录后重发。");
        } else {
          setError("账号已创建，但无法确认验证邮件发送；请登录后重发，请勿重复注册。");
        }
      }
    } catch {
      setError("未取得注册结果，请先确认账号状态后再重试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <span className="eyebrow">Create account</span>
        <h1>建立你的书架身份</h1>
        <p>注册后请先验证邮箱。未验证账号可以咨询，但不能准备订单或创建补货监听。</p>
        <label className="form-field">显示名称<input name="display_name" maxLength={80} required /></label>
        <label className="form-field">邮箱<input name="email" type="email" autoComplete="email" required /></label>
        <label className="form-field">密码<input name="password" type="password" autoComplete="new-password" minLength={8} maxLength={128} required /></label>
        {error ? <div className="error-banner" role="alert">{error}</div> : null}
        {message ? <div className="message assistant" role="status">{message}</div> : null}
        {created ? <Link href="/login">登录后重发验证邮件</Link> : null}
        <button className="primary" disabled={busy || created} type="submit">{busy ? "提交中…" : "注册并发送验证邮件"}</button>
        <div className="form-footer">已有账号？ <Link href="/login">去登录</Link></div>
      </form>
    </main>
  );
}
