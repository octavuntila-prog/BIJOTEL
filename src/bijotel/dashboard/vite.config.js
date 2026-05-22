// Vite config for the BIJOTEL dashboard.
//
// Development:
//   - `npm run dev` serves React at http://localhost:5173
//   - /api/* is proxied to bijotel serve on http://localhost:8080
//     so the dashboard never has to think about CORS or origins.
//
// Production:
//   - `npm run build` writes the static bundle to ../../../dashboard_dist
//     (alongside src/bijotel/, i.e. project root / dashboard_dist).
//   - Day 12 will wire `bijotel serve --dashboard` to mount these.

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        // Strip /api prefix when forwarding to bijotel serve, which
        // mounts routes at the root (e.g. /chain not /api/chain).
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  build: {
    outDir: '../../../dashboard_dist',
    emptyOutDir: true,
    sourcemap: false,
  },
})
