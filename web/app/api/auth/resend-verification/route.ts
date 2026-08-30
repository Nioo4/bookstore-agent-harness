import { accessToken } from "@/lib/server";
import { authError, forwardAuth, rejectCrossOrigin } from "@/lib/auth-server";

export async function POST(request: Request) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  if (new URL(request.url).search) return authError(400, "INVALID_ARGUMENT", "重发请求不接受查询参数");
  const token = await accessToken();
  if (!token) return authError(401, "AUTH_REQUIRED", "请先登录后重发");
  return forwardAuth(request, "/api/auth/resend-verification", token);
}
