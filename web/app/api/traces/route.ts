import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function GET(request: Request) {
  const token = await accessToken();
  if (!token) return authRequired();
  const query = new URL(request.url).search;
  return passJson(await fetch(`${agentApiUrl()}/api/traces${query}`, {
    headers: proxyHeaders(token, request),
    cache: "no-store",
  }));
}

function authRequired() {
  return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
}
