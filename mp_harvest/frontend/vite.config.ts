import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期后端地址：默认本机 FastAPI（shell/main.py 动态端口时可改用 VITE_API_TARGET 覆盖），
// 也可直接在页面 URL 上加 ?api=http://host:port 让浏览器直连后端（见 src/config.ts）。
const target = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.VITE_API_TARGET || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [vue()],
  // 相对路径：dist 放到任意目录/挂载子路径下资源都能正确加载（2026-08-09）
  base: './',
  server: {
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target, changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 600,
  },
})
