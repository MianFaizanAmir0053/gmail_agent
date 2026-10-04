import type { NextConfig } from "next";

/**
 * The chat page shows text from mail (M19, D7). It renders all of it as text,
 * and this policy lets it load no image from anywhere but this app, so no
 * text can make the browser call out.
 */
export const CHAT_POLICY = "img-src 'self'";

const config: NextConfig = {
  // Every page reads live trace data, so nothing may be statically cached at
  // build time -- a dashboard showing yesterday's numbers is worse than none.
  experimental: {},
  async headers() {
    return [
      { source: "/chat", headers: [{ key: "Content-Security-Policy", value: CHAT_POLICY }] },
    ];
  },
};

export default config;
