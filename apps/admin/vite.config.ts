import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/admin/",
  plugins: [vue()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  define: {
    // vue-i18n 编译期特性开关（使用 Composition API，关闭 legacy API）
    __VUE_I18N_FULL_INSTALL__: true,
    __VUE_I18N_LEGACY_API__: false,
    __INTLIFY_PROD_DEVTOOLS__: false,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": { target: "http://127.0.0.1:8100", changeOrigin: true },
      "/media": { target: "http://127.0.0.1:8100", changeOrigin: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("element-plus") || id.includes("@element-plus")) return "element-plus";
          if (id.includes("echarts") || id.includes("zrender")) return "echarts";
          if (id.includes("markdown-it") || id.includes("dompurify")) return "markdown";
          return "vendor";
        },
      },
    },
  },
});
