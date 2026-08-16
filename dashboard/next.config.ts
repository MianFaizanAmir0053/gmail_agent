import type { NextConfig } from "next";

const config: NextConfig = {
  // Every page reads live trace data, so nothing may be statically cached at
  // build time -- a dashboard showing yesterday's numbers is worse than none.
  experimental: {},
};

export default config;
