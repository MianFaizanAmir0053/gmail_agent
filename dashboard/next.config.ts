import type { NextConfig } from "next";

/**
 * Pages show text from mail (M19, D7). They render all of it as text, and
 * this policy lets them load no image from anywhere but this app, so no text
 * can make the browser call out.
 *
 * It is sent on every page, not only on Ask: a client navigation, or a Back
 * after a reload, keeps the first page's document, and so its policy.
 * Sign-in is left out. It shows no mail, and Auth.js loads its provider logos
 * from authjs.dev.
 */
export const IMAGE_POLICY = "img-src 'self'";

const config: NextConfig = {
  // Every page reads live trace data, so nothing may be statically cached at
  // build time -- a dashboard showing yesterday's numbers is worse than none.
  experimental: {},
  async headers() {
    return [
      {
        source: "/((?!api/auth/).*)",
        headers: [{ key: "Content-Security-Policy", value: IMAGE_POLICY }],
      },
    ];
  },
};

export default config;
