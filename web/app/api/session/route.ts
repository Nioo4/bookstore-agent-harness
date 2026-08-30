import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { accessToken, DISPLAY_NAME_COOKIE } from "@/lib/server";

export async function GET() {
  const authenticated = Boolean(await accessToken());
  const displayName = authenticated ? (await cookies()).get(DISPLAY_NAME_COOKIE)?.value?.trim() : "";
  return NextResponse.json(
    { authenticated, user: displayName ? { display_name: displayName } : null },
    { headers: { "Cache-Control": "no-store" } },
  );
}
