import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The FastAPI base URL is read server-side only (see src/lib/api.ts). It is
  // deliberately NOT a NEXT_PUBLIC_ variable: the backend has no authentication
  // yet, so the browser must never learn how to reach it directly.
  reactStrictMode: true,
};

export default nextConfig;
