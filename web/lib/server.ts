import { cookies } from "next/headers";
import { isIP } from "node:net";

export const ACCESS_COOKIE = "bookstore_access";
export const DISPLAY_NAME_COOKIE = "bookstore_display_name";

export function secureAccessCookie(): boolean {
  const configured = process.env.COOKIE_SECURE?.trim().toLowerCase();
  if (configured === "true") return true;
  if (configured === "false") return false;
  return process.env.NODE_ENV === "production";
}

export function goApiUrl(): string {
  const value = process.env.GO_API_URL;
  if (!value) throw new Error("GO_API_URL is required");
  return value.replace(/\/$/, "");
}

export function agentApiUrl(): string {
  const value = process.env.AGENT_API_URL;
  if (!value) throw new Error("AGENT_API_URL is required");
  return value.replace(/\/$/, "");
}

export async function accessToken(): Promise<string | undefined> {
  return (await cookies()).get(ACCESS_COOKIE)?.value;
}

export function proxyHeaders(token?: string, request?: Request): Record<string, string> {
  const forwarded: Record<string, string> = {};
  if (request && process.env.TRUST_PROXY_HEADERS === "true") {
    // Only enable behind the private reverse proxy, which overwrites both headers.
    const ip = request.headers.get("x-real-ip");
    if (ip && isIP(ip)) {
      forwarded["X-Forwarded-For"] = ip;
      forwarded["X-Real-IP"] = ip;
    }
  }
  const parent = request?.headers.get("traceparent");
  if (parent && /^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$/.test(parent)) {
    forwarded.traceparent = parent;
  }
  return {
    "Content-Type": "application/json",
    ...forwarded,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export async function passJson(response: Response): Promise<Response> {
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json", "Cache-Control": "no-store",
      ...(response.headers.has("Retry-After") ? { "Retry-After": response.headers.get("Retry-After")! } : {}),
    },
  });
}
