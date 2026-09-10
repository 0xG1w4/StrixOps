import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

export default function config(phase: string): NextConfig {
  return {
    output: "export",
    // Keep dev chunks separate from the static-export build cache.
    distDir: phase === PHASE_DEVELOPMENT_SERVER ? ".next-dev" : ".next",
    ...(phase === PHASE_DEVELOPMENT_SERVER ? {
      async rewrites() {
        const apiOrigin = (process.env.STRIXOPS_MCP_DEV_API || "http://127.0.0.1:8300").replace(/\/$/, "");
        return [{ source: "/api/mcp/:path*", destination: `${apiOrigin}/api/mcp/:path*` }];
      },
    } : {}),
  };
}
