import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// API_PROXY_TARGET lets a second backend (another port) be used without editing this file.
const target = process.env.API_PROXY_TARGET || 'http://127.0.0.1:8100'

export default defineConfig({
  plugins: [vue()],
  server: { strictPort: true, proxy: { '/api': { target, changeOrigin: true, rewrite: path => path.replace(/^\/api/, '') } } }
})
