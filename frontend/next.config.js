/** @type {import('next').NextConfig} */
const apiBackend = process.env.API_BACKEND_URL || 'http://localhost:8000';
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${apiBackend}/:path*` },
    ];
  },
};

module.exports = nextConfig;
