import { goApiUrl, proxyHeaders } from "./server";

export function authError(status: number, code: string, message: string): Response {
  return Response.json({ error: { code, message, retryable: status === 429 || status === 503 } },
    { status, headers: { "Cache-Control": "no-store" } });
}

export function rejectCrossOrigin(request: Request): Response | undefined {
  let expected: string;
  try {
    const configured = new URL(process.env.APP_BASE_URL ?? "");
    if (!["http:", "https:"].includes(configured.protocol) || configured.username || configured.password) throw new Error();
    expected = configured.origin;
  } catch {
    return authError(503, "DEPENDENCY_UNAVAILABLE", "应用来源配置不可用");
  }
  const origin = request.headers.get("origin");
  if (origin === expected || (!origin && request.headers.get("sec-fetch-site") === "same-origin")) return;
  return authError(403, "REQUEST_ORIGIN_DENIED", "请从本站页面提交请求");
}

export function authWebTimeout(): number {
  const values = [["SMTP_SEND_TIMEOUT", "5s"], ["AUTH_RPC_TIMEOUT", "10s"], ["AUTH_HTTP_TIMEOUT", "15s"], ["AUTH_WEB_TIMEOUT", "20s"]].map(([key, fallback]) => {
    const match = /^(\d+(?:\.\d+)?)(ms|s|m)$/.exec(process.env[key] ?? fallback);
    if (!match) throw new Error("invalid authentication deadline configuration");
    return Number(match[1]) * ({ ms: 1, s: 1000, m: 60000 }[match[2]]!);
  });
  if (values.some((value, index) => !Number.isFinite(value) || value <= 0 || (index > 0 && value <= values[index - 1]))) throw new Error("invalid authentication deadline chain");
  return Math.ceil(values[3]);
}

// One shared budget includes request-body consumption and response parsing.
// No retry and no credential-bearing redirects. Errors never echo input.
export async function forwardAuth(request: Request, path: string, token?: string): Promise<Response> {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  try {
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(authWebTimeout())]);
    const reader = request.body?.getReader();
    let body = "";
    if (reader) {
      const decoder = new TextDecoder();
      let bytes = 0;
      const onAbort = () => { void reader.cancel().catch(() => undefined); };
      signal.addEventListener("abort", onAbort, { once: true });
      try {
        while (true) {
          signal.throwIfAborted();
          const item = await reader.read();
          if (item.done) break;
          bytes += item.value.byteLength;
          if (bytes > 128 * 1024) {
            await reader.cancel();
            return authError(400, "INVALID_ARGUMENT", "请求体过大");
          }
          body += decoder.decode(item.value, { stream: true });
        }
        body += decoder.decode();
      } finally { signal.removeEventListener("abort", onAbort); reader.releaseLock(); }
    }
    signal.throwIfAborted();
    const upstream = await fetch(`${goApiUrl()}${path}`, {
      method: "POST", headers: { ...proxyHeaders(token, request), "Content-Type": request.headers.get("content-type") ?? "" },
      body, cache: "no-store", signal, redirect: "error",
    });
    const responseBody = await upstream.text();
    const headers: Record<string, string> = { "Content-Type": "application/json", "Cache-Control": "no-store" };
    const retry = upstream.headers.get("retry-after");
    if (retry && /^\d+$/.test(retry)) headers["Retry-After"] = retry;
    return new Response(responseBody, { status: upstream.status, headers });
  } catch {
    return authError(503, "DEPENDENCY_UNAVAILABLE", "认证服务暂时不可用，请稍后手动重试");
  }
}
