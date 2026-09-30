/**
 * Who may use the app, and which paths skip the check.
 *
 * No framework imports, so `node --test` loads this file as it is: the tests
 * run the same functions the proxy and the sign-in callback call, not a copy
 * of their rules.
 */

/**
 * Fetched by browsers and phones without the session cookie. A manifest
 * request carries no cookies, and iOS needs the manifest and icons to install
 * a real web app rather than a bookmark. Auth.js serves its own pages under
 * `/api/auth/`, and they must be reachable before anyone has signed in.
 */
const EXEMPT_PATHS = new Set(["/manifest.webmanifest", "/sw.js"]);
const EXEMPT_PREFIXES = ["/icons/", "/api/auth/"];

export function isExemptPath(pathname: string): boolean {
  return EXEMPT_PATHS.has(pathname) || EXEMPT_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

/** The claims of a Google profile this module reads. */
export type SignInProfile = {
  email?: string | null;
  email_verified?: boolean | null;
};

/**
 * Whether a Google sign-in may create a session: only a verified address
 * equal to `OWNER_EMAIL`. A blank or unset `OWNER_EMAIL` admits nobody -- an
 * unconfigured allow-list must never mean an open one.
 */
export function admitsSignIn(profile: SignInProfile | undefined, ownerEmail: string | undefined): boolean {
  return profile?.email_verified === true && sameAddress(profile.email, ownerEmail);
}

/**
 * Whether an existing session still belongs to the owner.
 *
 * Checked on every request as well as at sign-in, so changing `OWNER_EMAIL`
 * shuts out sessions issued to the previous address.
 */
export function isOwnerSession(email: string | null | undefined, ownerEmail: string | undefined): boolean {
  return sameAddress(email, ownerEmail);
}

function sameAddress(email: string | null | undefined, ownerEmail: string | undefined): boolean {
  const owner = ownerEmail?.trim().toLowerCase();
  if (!owner) {
    return false;
  }
  return email?.trim().toLowerCase() === owner;
}
