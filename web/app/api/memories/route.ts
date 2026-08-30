import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

async function forward(request: Request) {
  const token = await accessToken();
  if (!token) return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
  return passJson(await fetch(`${agentApiUrl()}/api/memories${new URL(request.url).search}`, {
    method: request.method, headers: proxyHeaders(token, request), cache: "no-store",
  }));
}

export const GET = forward;
export const DELETE = forward;
