"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import ResendVerification from "./resend-verification";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [unverified, setUnverified] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setError(payload?.error?.message ?? "登录失败");
      setBusy(false);
      return;
    }
    if (payload.user?.email_verified === false) {
      setUnverified(true);
      setBusy(false);
    } else { router.push("/"); }
    } catch { setError("未取得登录结果，请稍后重试。"); setBusy(false); }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <span className="eyebrow">Welcome back</span>
        <h1>继续你的阅读旅程</h1>
        <p>登录后可以准备订单、查看历史订单、保存和管理长期偏好。下单及创建补货监听前需先验证邮箱。</p>
        <label className="form-field">邮箱<input name="email" type="email" autoComplete="email" required /></label>
        <label className="form-field">密码<input name="password" type="password" autoComplete="current-password" minLength={8} required /></label>
        {error ? <div className="error-banner" role="alert">{error}</div> : null}
        <button className="primary" disabled={busy} type="submit">{busy ? "登录中…" : "登录"}</button>
        <div className="form-footer">还没有账号？ <Link href="/register">创建账号</Link></div>
        {unverified ? <ResendVerification /> : null}
      </form>
    </main>
  );
}
