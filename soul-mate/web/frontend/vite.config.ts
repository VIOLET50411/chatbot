import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 生产构建输出到 ../static，与 web/app.py 的 StaticFiles 挂载路径一致。
// 注意：验证阶段请使用 `npm run build:local`（输出到本地 dist/，不清空旧 static）。
export default defineConfig({
  plugins: [react()],
  base: '/static/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
