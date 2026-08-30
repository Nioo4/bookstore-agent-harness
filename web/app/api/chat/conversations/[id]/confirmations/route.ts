import { accessToken, agentApiUrl, passJson, proxyHeaders } from "@/lib/server";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const { id } = await context.params;
  const token = await accessToken();
  const upstream = await fetch(`${agentApiUrl()}/api/chat/conversations/${encodeURIComponent(id)}/confirmations`, {
    method: "POST",
    headers: proxyHeaders(token, request),
    body: await request.text(),
    cache: "no-store",
  });
  return passJson(upstream);
}
import { rejectCrossOrigin } from "@/lib/auth-server";
