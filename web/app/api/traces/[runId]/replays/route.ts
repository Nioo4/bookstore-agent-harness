import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function GET(request: Request, context: { params: Promise<{ runId: string }> }) {
  const token = await accessToken();
  if (!token) return authRequired();
  const { runId } = await context.params;
  const query = new URL(request.url).search;
  return passJson(await fetch(`${agentApiUrl()}/api/traces/${encodeURIComponent(runId)}/replays${query}`, {
    headers: proxyHeaders(token, request),
    cache: "no-store",
  }));
}

export async function POST(request: Request, context: { params: Promise<{ runId: string }> }) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const token = await accessToken();
  if (!token) return authRequired();
  const { runId } = await context.params;
  const idempotencyKey = request.headers.get("Idempotency-Key") ?? "";
  const body = await request.text();
  return passJson(await fetch(`${agentApiUrl()}/api/traces/${encodeURIComponent(runId)}/replays`, {
    method: "POST",
    headers: { ...proxyHeaders(token, request), "Idempotency-Key": idempotencyKey },
    body,
    cache: "no-store",
  }));
}

function authRequired() {
  return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
}
import { rejectCrossOrigin } from "@/lib/auth-server";
