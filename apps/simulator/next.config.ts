import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // HDR_STATIC_EXPORT=1 builds plain files into out/ for the single-port hosted server (python -m hdr.hosted).
  ...(process.env.HDR_STATIC_EXPORT === "1" ? { output: "export" as const } : {}),
};

export default config;
