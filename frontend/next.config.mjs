/** @type {import('next').NextConfig} */
const nextConfig = {
  async redirects() {
    return [
      {
        source: "/strategy-lab",
        destination: "/lab",
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
