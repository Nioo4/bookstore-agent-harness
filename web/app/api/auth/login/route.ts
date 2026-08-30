import { NextResponse } from "next/server";
import { ACCESS_COOKIE, DISPLAY_NAME_COOKIE, secureAccessCookie } from "@/lib/server";
import { authError, forwardAuth } from "@/lib/auth-server";

export async function POST(request: Request) {
  const upstream = await forwardAuth(request, "/api/auth/login");
  if (!upstream.ok) return upstream;
  const payload = await upstream.json().catch(() => ({}));

  const { access_token: accessToken, expires_in: expiresIn, ...safePayload } = payload;
  if (typeof accessToken !== "string" || !accessToken) {
    return authError(503, "DEPENDENCY_UNAVAILABLE", "登录响应无效");
  }
  const response = NextResponse.json(safePayload, { headers: { "Cache-Control": "no-store" } });
  response.cookies.set(ACCESS_COOKIE, accessToken, {
    httpOnly: true,
    sameSite: "lax",
    secure: secureAccessCookie(),
    path: "/",
    maxAge: Number(expiresIn) || 8 * 60 * 60,
  });
  const displayName = typeof payload?.user?.display_name === "string" ? payload.user.display_name.trim() : "";
  if (displayName && displayName.length <= 100) {
    response.cookies.set(DISPLAY_NAME_COOKIE, displayName, {
      httpOnly: true,
      sameSite: "lax",
      secure: secureAccessCookie(),
      path: "/",
      maxAge: Number(expiresIn) || 8 * 60 * 60,
    });
  } else {
    // A malformed upstream response must not leave the previous account name
    // attached to the newly issued access cookie.
    response.cookies.set(DISPLAY_NAME_COOKIE, "", {
      httpOnly: true,
      sameSite: "lax",
      secure: secureAccessCookie(),
      path: "/",
      maxAge: 0,
    });
  }
  return response;
}
