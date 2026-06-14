import type { Config } from 'tailwindcss';

/**
 * Design tokens — the single source of truth for color across the app.
 *
 * Use SEMANTIC names everywhere (bg-surface-raised, text-content-muted,
 * text-success, border-line) instead of raw palette classes (bg-zinc-800,
 * text-emerald-400). That way the whole look changes by editing this file, not
 * by grepping 60 pages.
 *
 * - surface.*  → backgrounds, layered by elevation (base < raised < overlay)
 * - content.*  → text, by emphasis (primary > secondary > muted)
 * - line.*     → borders / dividers
 * - brand      → the indigo identity (primary actions, active nav)
 * - success/warning/danger/info → status & intent (hex so bg-success/10 works)
 */
const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#6366f1',
          hover: '#5558e3',
          50: '#eef2ff',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
        },
        surface: {
          DEFAULT: '#0f1117',
          base: '#0f1117',
          raised: '#1a1d2e',
          overlay: '#242736',
          inset: '#0b0d14',
        },
        content: {
          DEFAULT: '#e4e4e7',
          primary: '#e4e4e7',
          secondary: '#a1a1aa',
          muted: '#71717a',
        },
        line: {
          DEFAULT: 'rgba(255,255,255,0.10)',
          subtle: 'rgba(255,255,255,0.06)',
          strong: 'rgba(255,255,255,0.16)',
        },
        // Status / intent — hex so opacity modifiers (bg-success/10) work.
        success: '#34d399',
        warning: '#fbbf24',
        danger: '#f87171',
        info: '#60a5fa',
      },
      borderRadius: {
        card: '12px',
      },
    },
  },
  plugins: [],
};

export default config;
