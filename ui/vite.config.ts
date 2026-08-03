import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { tanstackRouter } from '@tanstack/router-plugin/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = env.VITE_API_PROXY_TARGET || 'http://localhost:9999'

  return {
    base: '/static/app/',
    plugins: [tanstackRouter({ target: 'react', autoCodeSplitting: true }), react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': backend,
        '/health': backend,
        '/static/img': backend,
      },
    },
    build: {
      outDir: '../intg-manager/static/app',
      emptyOutDir: true,
      sourcemap: false,
    },
  }
})
