import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端不直接访问模型端点：所有请求经后端 /api/v1 转发，避免暴露密钥与形成两套调用路径
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
