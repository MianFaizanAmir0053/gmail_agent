import { NextResponse, type NextRequest } from "next/server";

const COOKIE = "mailagent_dash";

/**
 * Shared-token gate.
 *
 * Deliberately not a user system. This dashboard has exactly one reader, and
 * accounts, sessions, and password resets would cost days while demonstrating
 * nothing the rest of the project does not already show.
 *
 * A token in the query string is exchanged for a cookie and then removed from
 * the URL, so the secret does not sit in browser history or in a referrer
 * header on every subsequent navigation.
 */
export function middleware(request: NextRequest) {
  const expected = process.env.DASHBOARD_TOKEN;

  // Fail closed. An unset token must not mean "no authentication required".
  if (!expected) {
    return new NextResponse("DASHBOARD_TOKEN is not configured", { status: 503 });
  }

  if (request.cookies.get(COOKIE)?.value === expected) {
    return NextResponse.next();
  }

  const supplied = request.nextUrl.searchParams.get("token");
  if (supplied === expected) {
    const url = request.nextUrl.clone();
    url.searchParams.delete("token");
    const response = NextResponse.redirect(url);
    response.cookies.set(COOKIE, expected, {
      httpOnly: true,
      sameSite: "lax",
      // Derived from the request, not from NODE_ENV. Keying it on NODE_ENV
      // makes `npm start` on http://localhost set a Secure cookie the browser
      // then refuses to send back, so sign-in silently never completes -- while
      // still behaving correctly once actually served over HTTPS.
      secure: request.nextUrl.protocol === "https:",
      path: "/",
      maxAge: 60 * 60 * 24 * 30,
    });
    return response;
  }

  return new NextResponse("Unauthorised. Append ?token=… once to sign in.", {
    status: 401,
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
