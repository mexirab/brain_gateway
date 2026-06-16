// Adds jest-dom matchers (toBeInTheDocument, toHaveClass, …) to Vitest's
// expect, and the global type augmentation so `tsc` type-checks them too.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// Unmount React trees between tests (we don't enable Vitest `globals`, so RTL's
// automatic cleanup isn't registered — do it explicitly to avoid DOM bleed).
afterEach(() => {
  cleanup();
});
