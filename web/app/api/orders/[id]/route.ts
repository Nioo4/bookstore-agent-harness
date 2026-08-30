import { accessToken, goApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  const token = await accessToken();
  if (!token) return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
  const { id } = await context.params;
  return passJson(await fetch(`${goApiUrl()}/api/orders/${encodeURIComponent(id)}`, { headers: proxyHeaders(token, request), cache: "no-store" }));
}
