import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/manifests': 'http://localhost:8000',
      '/analysis-tasks': 'http://localhost:8000',
      '/calibrations': 'http://localhost:8000',
      '/reports': 'http://localhost:8000',
      '/health': 'http://localhost:8000'
    }
  }
})
