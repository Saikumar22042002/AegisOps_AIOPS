/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  poweredByHeader: false,
  eslint: { ignoreDuringBuilds: false },
};

export default nextConfig;
