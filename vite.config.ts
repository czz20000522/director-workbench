import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:4100', '/openapi.json': 'http://127.0.0.1:4100', '/media-file': 'http://127.0.0.1:4100', '/material-file': 'http://127.0.0.1:4100', '/workflow-file': 'http://127.0.0.1:4100' } },
});
