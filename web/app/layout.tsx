import type { Metadata } from "next";
import Link from "next/link";
import Navigation from "./navigation";
import "./globals.css";

export const metadata: Metadata = {
  title: "页间 · 智能书店",
  description: "由可验证 Agent 协作驱动的书籍咨询、推荐与安全下单体验。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="topbar">
          <Link className="brand" href="/" aria-label="页间智能书店首页">
            <span className="brand-mark" aria-hidden="true">页</span>
            <span><strong>页间</strong><small>智能书店</small></span>
          </Link>
          <Navigation />
        </header>
        {children}
      </body>
    </html>
  );
}
