import { forwardAuth } from "@/lib/auth-server";

export async function POST(request: Request) {
  return forwardAuth(request, "/api/auth/register");
}
