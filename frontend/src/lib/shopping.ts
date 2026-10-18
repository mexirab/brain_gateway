import type { ShoppingItem } from './types';

/** Split a list into what's still to buy (newest first, as the API orders it)
 *  and what's been bought (most recently ticked first, so an accidental tap is
 *  right at the top of the Bought section). */
export function splitShopping(items: ShoppingItem[]): { toBuy: ShoppingItem[]; bought: ShoppingItem[] } {
  const toBuy = items.filter((it) => !it.checked);
  const bought = items
    .filter((it) => it.checked)
    .sort((a, b) => (b.checked_at ?? '').localeCompare(a.checked_at ?? ''));
  return { toBuy, bought };
}
