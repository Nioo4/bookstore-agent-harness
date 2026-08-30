import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function DELETE(request: Request, context: { params: Promise<{ id: string }> }) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const token = await accessToken();
  if (!token) return Response.json({ error: { code: "AUTH_REQUIRED", message: "请先登录", retryable: false, trace_id: "" } }, { status: 401 });
  const { id } = await context.params;
  return passJson(await fetch(`${agentApiUrl()}/api/memories/${encodeURIComponent(id)}`, {
    method: "DELETE", headers: proxyHeaders(token, request), cache: "no-store",
  }));
}
import { rejectCrossOrigin } from "@/lib/auth-server";
