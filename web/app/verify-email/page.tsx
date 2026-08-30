"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type VerificationResult = { ok: boolean; message: string };
// Only the safe result/promise is shared across React remounts. Never store
// the token in component state, localStorage, history state or a cache key.
let currentAttempt: Promise<VerificationResult> | undefined;

function consumeLink(): Promise<VerificationResult> {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (url.search) window.history.replaceState(null, "", url.pathname);
  if (token) {
    currentAttempt = fetch("/api/auth/verify-email", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
    }).then(async (response) => {
      const payload = await response.json();
      if (response.ok) return { ok: true, message: "邮箱验证成功，请重新登录更新凭证后使用订单和补货监听功能。" };
      const messages: Record<string, string> = {
        EMAIL_VERIFICATION_INVALID: "验证链接无效，请登录后重发。",
        EMAIL_VERIFICATION_EXPIRED: "验证链接已过期，请登录后重发。",
        EMAIL_VERIFICATION_ALREADY_USED: "链接已使用或邮箱已验证，请重新登录；仍未验证时可登录后重发。",
      };
      return { ok: false, message: messages[payload?.error?.code] ?? "暂未取得验证结果，请重新登录确认邮箱状态。" };
    }).catch(() => ({ ok: false, message: "暂未取得验证结果，请重新登录确认邮箱状态。" }));
  }
  return currentAttempt ?? Promise.resolve({ ok: false, message: "验证链接缺少令牌，请登录后重发。" });
}

export default function VerifyEmailPage() {
  const [result, setResult] = useState<VerificationResult>({ ok: false, message: "正在验证邮箱…" });
  useEffect(() => {
    let active = true;
    void consumeLink().then((value) => { if (active) setResult(value); });
    return () => { active = false; };
  }, []);
  return <main className="auth-page"><section className="auth-card">
    <span className="eyebrow">Email verification</span>
    <h1>{result.ok ? "验证完成" : "验证邮箱"}</h1>
    <p role="status">{result.message}</p>
    <Link className="primary" href="/login">{result.ok ? "重新登录" : "登录后重发 / 确认验证状态"}</Link>
  </section></main>;
}
