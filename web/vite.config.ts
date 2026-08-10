import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Chỉ dùng khi chạy `npm run dev` ngoài cụm; trong cụm là Ingress lo.
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
  },
  build: {
    // `scripts/check-bundle-splitting.mjs` (phép canh "Monaco chỉ tải trì hoãn", chạy qua
    // `make bundle-check`) đọc `dist/.vite/manifest.json` để tìm ĐÚNG chunk entry —
    // đáng tin hơn đoán theo tên file (`index-*.js`), vì tên file mang hash đổi mỗi build
    // và một chunk khác cũng có thể trùng tiền tố "index" nếu code-splitting đổi cách chia.
    manifest: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
  },
})
