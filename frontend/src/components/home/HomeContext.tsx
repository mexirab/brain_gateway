'use client';

import { createContext, useContext } from 'react';
import type { HAEntity } from '@/lib/types';

export interface HomeControls {
  /** Send one HA service call. `patch` is applied optimistically and rolled
   *  back if the call fails. Resolves true on success. */
  run: (
    entity: HAEntity,
    service: string,
    data?: Record<string, unknown>,
    patch?: Partial<HAEntity>,
  ) => Promise<boolean>;
  /** Per-entity error message from the last failed call (cleared on retry). */
  errors: Record<string, string>;
  /** Entities with a call in flight. */
  pending: Record<string, boolean>;
}

export const HomeControlsContext = createContext<HomeControls | null>(null);

export function useHomeControls(): HomeControls {
  const ctx = useContext(HomeControlsContext);
  if (!ctx) throw new Error('useHomeControls must be used inside HomeControlsContext');
  return ctx;
}
