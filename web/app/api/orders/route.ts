import { accessToken, goApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function GET(request: Request) {
  const token = await accessToken();
  if (!token) return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
  const query = new URL(request.url).search;
  return passJson(await fetch(`${goApiUrl()}/api/orders${query}`, { headers: proxyHeaders(token, request), cache: "no-store" }));
}
