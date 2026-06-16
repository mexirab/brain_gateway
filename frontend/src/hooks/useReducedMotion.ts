'use client';

import { useEffect, useState } from 'react';

/**
 * Tracks the user's `prefers-reduced-motion` OS setting.
 *
 * SSR-safe: returns `false` on the server and on the first client render, then
 * syncs to the real media-query value after mount (and on live changes). Use it
 * to skip non-essential celebration animations — XP toasts, level-up overlays,
 * boss-battle flourishes — for neurodivergent users who opt out of motion.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  return reduced;
}
