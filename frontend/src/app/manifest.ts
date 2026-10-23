import type { MetadataRoute } from 'next';
import { getAssistantName } from '@/lib/branding.server';
import { THEME_BACKGROUND } from '@/styles/theme-meta';

/**
 * PWA manifest (served at /manifest.webmanifest). Makes the dashboard
 * installable on a phone (standalone, no browser chrome). The name follows the
 * assistant name from /settings → Identity; the splash color is the dark
 * palette's page background (the default theme).
 */
export default async function manifest(): Promise<MetadataRoute.Manifest> {
  const name = await getAssistantName();
  return {
    name,
    short_name: name,
    description: 'Personal AI assistant for ADHD support',
    start_url: '/dashboard',
    display: 'standalone',
    orientation: 'any',
    background_color: THEME_BACKGROUND.dark,
    theme_color: THEME_BACKGROUND.dark,
    // PNGs are required for iOS home-screen install (needed for Web Push);
    // the SVG stays for browsers that prefer it.
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
      { src: '/icons/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
    ],
  };
}
