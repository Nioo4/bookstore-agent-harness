import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function GET(request: Request, context: { params: Promise<{ runId: string }> }) {
  const token = await accessToken();
  if (!token) return authRequired();
  const { runId } = await context.params;
  const query = new URL(request.url).search;
  return passJson(await fetch(`${agentApiUrl()}/api/traces/${encodeURIComponent(runId)}${query}`, {
    headers: proxyHeaders(token, request),
    cache: "no-store",
  }));
}

export async function DELETE(request: Request, context: { params: Promise<{ runId: string }> }) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const token = await accessToken();
  if (!token) return authRequired();
  const { runId } = await context.params;
  return passJson(await fetch(`${agentApiUrl()}/api/traces/${encodeURIComponent(runId)}`, {
    method: "DELETE",
    headers: proxyHeaders(token, request),
    cache: "no-store",
  }));
}

function authRequired() {
  return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
}
import { rejectCrossOrigin } from "@/lib/auth-server";
