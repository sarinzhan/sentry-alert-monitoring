import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build lands in app/web/ so FastAPI serves it at /web/ and the existing
// `COPY app/ ./app/` in the Dockerfile picks it up — build the UI first.
export default defineConfig({
  plugins: [react()],
  base: '/web/',
  build: {
    outDir: '../app/web',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8080',
    },
  },
})
