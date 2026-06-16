import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

// `.mts` so Vite loads this as ESM. We rely on Vitest's built-in esbuild with
// the automatic JSX runtime (no per-file `import React`), matching how Next
// compiles the app — so no @vitejs/plugin-react is needed.
export default defineConfig({
  esbuild: {
    jsx: 'automatic',
    jsxImportSource: 'react',
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
});
