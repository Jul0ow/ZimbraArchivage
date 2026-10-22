import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Image Docker minimale : seul le serveur autonome est embarqué
  output: "standalone",
  poweredByHeader: false,
};

export default nextConfig;
