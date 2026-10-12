import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'ws://127.0.0.1:8765',
        ws: true,
      },
      '/audio': {
        target: 'http://127.0.0.1:8765',
      },
      '/api': {
        target: 'http://127.0.0.1:8765',
      },
    },
  },
});
