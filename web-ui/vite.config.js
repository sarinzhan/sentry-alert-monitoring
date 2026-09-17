import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Everything lives under /admin-web/ so the UI can hang off an existing
// domain (superapp-sentry.beeline.kg/admin-web/) without colliding with
// Sentry's own routes (/api/ especially). The bot's API stays at /api/ —
// the nginx in the sentry-web container strips the prefix.
export default defineConfig({
  plugins: [react()],
  base: '/admin-web/',
  build: {
    outDir: '../app/web',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/admin-web/api': {
        target: 'http://localhost:8080',
        rewrite: (path) => path.replace(/^\/admin-web/, ''),
      },
    },
  },
})
