const { startServer } = require("./node_modules/next/dist/server/lib/start-server");

startServer({
  dir: __dirname,
  port: 3000,
  allowRetry: false,
  isDev: true,
  hostname: "0.0.0.0"
}).then(() => {
  console.log("Ready on http://localhost:3000");
}).catch((err) => {
  console.error("Failed to start dev server:", err);
  process.exit(1);
});
