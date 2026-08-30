"use client";

import Link from "next/link";
import { KeyboardEvent, useEffect, useRef, useState } from "react";

type Session = {
  authenticated: boolean;
  user: { display_name: string } | null;
};

const signedOut: Session = { authenticated: false, user: null };

export default function Navigation() {
  const [session, setSession] = useState<Session | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const logoutRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/session", { cache: "no-store", signal: controller.signal })
      .then(async (response) => response.ok ? response.json() as Promise<Session> : signedOut)
      .then((value) => {
        if (!controller.signal.aborted) setSession(value.authenticated ? value : signedOut);
      })
      .catch(() => {
        if (!controller.signal.aborted) setSession(signedOut);
      });
    return () => controller.abort();
  }, []);

  function focusLogout() {
    requestAnimationFrame(() => logoutRef.current?.focus());
  }

  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Escape") {
      setMenuOpen(false);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setMenuOpen(true);
      focusLogout();
    }
  }

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      setMenuOpen(false);
      triggerRef.current?.focus();
    }
  }

  async function logout() {
    setLoggingOut(true);
    setError("");
    try {
      const response = await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error("logout failed");
      setSession(signedOut);
      setMenuOpen(false);
      window.location.assign("/");
    } catch {
      setError("登出未完成，请稍后重试。");
      setLoggingOut(false);
    }
  }

  return (
    <nav aria-label="主导航">
      <Link href="/">咨询</Link>
      <Link href="/orders">我的订单</Link>
      <Link href="/tasks">补货监听</Link>
      <Link href="/notifications">通知</Link>
      <Link href="/memories">我的记忆</Link>
      <Link href="/traces">执行轨迹</Link>
      {session === null ? <span className="nav-session-placeholder" aria-label="正在获取登录状态">…</span> : null}
      {session?.authenticated ? (
        <div
          className="account-menu"
          onMouseEnter={() => setMenuOpen(true)}
          onMouseLeave={() => setMenuOpen(false)}
        >
          <button
            ref={triggerRef}
            className="account-trigger"
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={onTriggerKeyDown}
          >
            {session.user?.display_name || "账户"}
          </button>
          {menuOpen ? (
            <div className="account-popover" role="menu" aria-label="账户菜单" onKeyDown={onMenuKeyDown}>
              <button ref={logoutRef} type="button" role="menuitem" onClick={logout} disabled={loggingOut}>
                {loggingOut ? "登出中…" : "登出"}
              </button>
            </div>
          ) : null}
          {error ? <span className="account-error" role="status">{error}</span> : null}
        </div>
      ) : session ? <Link href="/login">登录</Link> : null}
    </nav>
  );
}
