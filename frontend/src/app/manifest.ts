import type { MetadataRoute } from 'next';

/**
 * PWA manifest (served at /manifest.webmanifest). Makes the dashboard
 * installable — useful for the Callisto Pi kiosk (launches standalone, no
 * browser chrome) and for installing on a phone. Colors mirror the design
 * tokens (surface-base background, theme color matches the status bar).
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'Convivial Prophet — Jess',
    short_name: 'Jess',
    description: 'Personal AI assistant for ADHD support',
    start_url: '/dashboard',
    display: 'standalone',
    orientation: 'any',
    background_color: '#0b0c12',
    theme_color: '#0b0c12',
    icons: [
      {
        src: '/icons/icon.svg',
        sizes: 'any',
        type: 'image/svg+xml',
        purpose: 'any',
      },
    ],
  };
}
