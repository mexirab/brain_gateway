import type { Metadata, Viewport } from 'next';
import { Inter } from 'next/font/google';
import { getAssistantName } from '@/lib/branding.server';
import { getThemePref } from '@/lib/theme.server';
import { THEME_BACKGROUND } from '@/styles/theme-meta';
import './globals.css';

const inter = Inter({ subsets: ['latin'] });

export async function generateMetadata(): Promise<Metadata> {
  const name = await getAssistantName();
  return {
    applicationName: name,
    title: name,
    description: 'Personal AI assistant for ADHD support, running on your own hardware',
    manifest: '/manifest.webmanifest',
    icons: {
      icon: '/icons/icon.svg',
      // iOS ignores SVG touch icons; the PNG is what the home-screen app shows.
      apple: '/icons/apple-touch-icon.png',
    },
    appleWebApp: {
      capable: true,
      // Translucent = edge-to-edge with white status-bar text, right for the
      // dark palette. Light/system get 'default' (iOS tints it from
      // theme-color with dark text). iOS reads this when the app is added to
      // the home screen — re-add after switching theme to update it.
      statusBarStyle: getThemePref() === 'dark' ? 'black-translucent' : 'default',
      title: name,
    },
  };
}

// Next 14 requires themeColor/viewport in a separate `viewport` export.
export function generateViewport(): Viewport {
  const pref = getThemePref();
  return {
    themeColor:
      pref === 'system'
        ? [
            { media: '(prefers-color-scheme: light)', color: THEME_BACKGROUND.light },
            { media: '(prefers-color-scheme: dark)', color: THEME_BACKGROUND.dark },
          ]
        : THEME_BACKGROUND[pref],
    width: 'device-width',
    initialScale: 1,
    maximumScale: 5,
    // Edge-to-edge on notched phones; the layouts pad with safe-area insets.
    viewportFit: 'cover',
  };
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-theme={getThemePref()}>
      <body className={`${inter.className} antialiased min-h-screen`}>
        {children}
      </body>
    </html>
  );
}
