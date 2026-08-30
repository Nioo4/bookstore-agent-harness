import { NextResponse } from "next/server";
import { ACCESS_COOKIE, DISPLAY_NAME_COOKIE } from "@/lib/server";
import { rejectCrossOrigin } from "@/lib/auth-server";

export async function POST(request: Request) {
  const denied = rejectCrossOrigin(request);
  if (denied) return denied;
  const response = NextResponse.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
  response.cookies.set(ACCESS_COOKIE, "", { httpOnly: true, maxAge: 0, path: "/", sameSite: "lax" });
  response.cookies.set(DISPLAY_NAME_COOKIE, "", { httpOnly: true, maxAge: 0, path: "/", sameSite: "lax" });
  return response;
}
