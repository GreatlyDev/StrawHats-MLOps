import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { resolveRuntime } from './src/runtime.ts';
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_');
  resolveRuntime(env.VITE_APP_MODE, env.VITE_API_BASE_URL);
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: { '/api': 'http://127.0.0.1:8000' },
    },
  };
});
