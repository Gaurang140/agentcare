import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const COOKIE_NAME = "access_token";

/**
 * Gates /portal/* and /staff/* on the mere presence of the access_token
 * cookie (an httpOnly cookie set by POST /api/auth/login - this proxy
 * cannot read its contents, only whether it exists). This is a UX
 * convenience only: it stops a logged-out browser from flashing a
 * protected page before redirecting. It proves nothing about the role
 * (patient vs staff) or whether the token is still valid - the backend's
 * `get_current_user` / `require_role` dependencies (backend/app/auth/
 * dependencies.py) are the only real access control. Never add
 * authorization logic here that the backend doesn't also enforce.
 */
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isProtected = pathname.startsWith("/portal") || pathname.startsWith("/staff");

  if (isProtected && !request.cookies.has(COOKIE_NAME)) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/portal/:path*", "/staff/:path*"],
};
