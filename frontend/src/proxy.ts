import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { LANG_COOKIE, hasLocale, pickLocale } from "@/i18n/config";

// Every page lives under /pa or /en. A path without a language is sent to the saved choice, else the browser's preference.
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const first = pathname.split("/")[1] ?? "";
  if (hasLocale(first)) {
    // No session cookie at all on a page that needs one: a real HTTP redirect to sign-in. This is only a courtesy
    // check; the page still asks the API whether the session is valid (a forged cookie gets nothing).
    const rest = pathname.slice(first.length + 1);
    const isPublic = rest === "/sign-in" || rest === "/status";
    if (!isPublic && !request.cookies.has("kn_session")) {
      const url = request.nextUrl.clone();
      url.pathname = `/${first}/sign-in`;
      url.search = "";
      return NextResponse.redirect(url);
    }
    const response = NextResponse.next();
    response.cookies.set(LANG_COOKIE, first, { path: "/", maxAge: 60 * 60 * 24 * 365, sameSite: "lax" });
    return response;
  }
  const locale = pickLocale(request.cookies.get(LANG_COOKIE)?.value, request.headers.get("accept-language"));
  const url = request.nextUrl.clone();
  url.pathname = `/${locale}${pathname === "/" ? "" : pathname}`;
  return NextResponse.redirect(url);
}

export const config = {
  // not for Next internals, the health probe of the container, or files with an extension (favicon, images)
  matcher: ["/((?!_next|api|favicon.ico|.*\\..*).*)"],
};
