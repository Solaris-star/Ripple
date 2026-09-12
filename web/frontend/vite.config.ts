import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:7860', '/static': 'http://127.0.0.1:7860' },
  },
  base: './',  // 相对路径，适配 proxy
})
