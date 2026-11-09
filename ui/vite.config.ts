import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 产物必须能由「本地文件 / 自托管 / Host 内嵌」三种方式加载，所以 base 用相对路径。
// 页面本身从 Host 的随机回环端口加载，RPC 用同源绝对路径 /rpc、/state、/events。
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    // 不生成 sourcemap：产物要提交，调试信息留本地看源码即可。
    sourcemap: false,
    assetsInlineLimit: 4096,
  },
  server: {
    host: "127.0.0.1",
    port: 5199,
    strictPort: true,
  },
});