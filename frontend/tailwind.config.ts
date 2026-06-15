import type { Config } from 'tailwindcss';

/**
 * Design tokens — the single source of truth for color across the app.
 *
 * Use SEMANTIC names everywhere (bg-surface-raised, text-content-muted,
 * text-success, border-line) instead of raw palette classes (bg-zinc-800,
 * text-emerald-400). That way the whole look changes by editing this file, not
 * by grepping 60 pages.
 *
 * Evolved palette (2026-06-14): warmer, deeper ink surfaces with a faint violet
 * undertone that ties to the brand; a more vivid indigo-violet brand; text
 * bumped to pass WCAG AA on dark; status hues harmonized with the brand. The
 * `accent.*` set keeps the finance "game island" playful while still being
 * token-driven.
 *
 * - surface.*  → backgrounds, layered by elevation (inset < base < raised < overlay)
 * - content.*  → text, by emphasis (primary > secondary > muted)
 * - line.*     → borders / dividers (subtle < DEFAULT < strong)
 * - brand      → the indigo-violet identity (primary actions, active nav)
 * - success/warning/danger/info → status & intent (hex so bg-success/10 works)
 * - accent.*   → gamification palette (XP, quests, streaks) for the finance/quest UI
 */
const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#6f63ff',
          hover: '#5d50f0',
          50: '#eef0ff',
          500: '#6f63ff',
          600: '#5d50f0',
          700: '#4a3fd6',
        },
        surface: {
          DEFAULT: '#0b0c12',
          base: '#0b0c12',
          raised: '#14151f',
          overlay: '#1d1e2b',
          inset: '#07080d',
        },
        content: {
          DEFAULT: '#edecf2',
          primary: '#edecf2',
          secondary: '#aaa9b8',
          muted: '#84838f',
        },
        line: {
          DEFAULT: 'rgba(255,255,255,0.09)',
          subtle: 'rgba(255,255,255,0.05)',
          strong: 'rgba(255,255,255,0.15)',
        },
        // Status / intent — hex so opacity modifiers (bg-success/10) work.
        success: '#34d399',
        warning: '#fbbf24',
        danger: '#fb7185',
        info: '#60a5fa',
        // Gamification accents — finance/quest "game island" character.
        accent: {
          gold: '#f5c451',
          violet: '#a78bfa',
          cyan: '#34d3e0',
          flame: '#fb7c4d',
        },
      },
      borderRadius: {
        card: '14px',
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.30), 0 8px 24px -12px rgba(0,0,0,0.55)',
        glow: '0 0 0 1px rgba(111,99,255,0.20), 0 8px 30px -8px rgba(111,99,255,0.35)',
      },
    },
  },
  plugins: [],
};

export default config;
