import type { NextConfig } from "next";
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  devIndicators: false,
  experimental: { workerThreads: true, useTypeScriptCli: false },
};
export default config;
