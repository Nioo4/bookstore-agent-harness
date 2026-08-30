import { accessToken, agentApiUrl, proxyHeaders } from "@/lib/server";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const { id } = await context.params;
  const token = await accessToken();
  const upstream = await fetch(`${agentApiUrl()}/api/chat/conversations/${encodeURIComponent(id)}/messages`, {
    method: "POST",
    headers: proxyHeaders(token, request),
    body: await request.text(),
    cache: "no-store",
    signal: request.signal,
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
import { rejectCrossOrigin } from "@/lib/auth-server";
