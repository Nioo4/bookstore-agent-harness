import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function POST(request: Request) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const token = await accessToken();
  if (!token) return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
  const headers = new Headers(proxyHeaders(token, request));
  const idempotencyKey = request.headers.get("Idempotency-Key");
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
  const upstream = await fetch(`${agentApiUrl()}/api/tasks/inventory-watches`, {
    method: "POST",
    headers,
    body: await request.text(),
    cache: "no-store",
  });
  return passJson(upstream);
}
import { rejectCrossOrigin } from "@/lib/auth-server";
