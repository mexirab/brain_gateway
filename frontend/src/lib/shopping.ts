import type { ShoppingItem } from './types';

/** Bought items older than this drop out of the "Bought" section (they stay in
 *  the database until "Clear bought"), so it shows this trip, not since April. */
export const BOUGHT_WINDOW_DAYS = 7;

/** Placeholder `checked_at` for an item ticked a moment ago, before the server
 *  answers: sorts above every real timestamp, so it lands at the top of Bought. */
export const JUST_BOUGHT = '9999-12-31T23:59:59';

/** Split a list into what's still to buy (API order) and what was bought in the
 *  last BOUGHT_WINDOW_DAYS (most recently ticked first, so an accidental tap is
 *  right at the top of the Bought section). */
export function splitShopping(
  items: ShoppingItem[],
  now: Date = new Date(),
): { toBuy: ShoppingItem[]; bought: ShoppingItem[] } {
  const cutoff = now.getTime() - BOUGHT_WINDOW_DAYS * 86_400_000;
  const toBuy = items.filter((it) => !it.checked);
  const bought = items
    .filter((it) => {
      if (!it.checked) return false;
      if (!it.checked_at || it.checked_at === JUST_BOUGHT) return true;
      const t = Date.parse(it.checked_at); // server time is naive local, parsed as local
      return Number.isNaN(t) || t >= cutoff;
    })
    .sort((a, b) => (b.checked_at ?? '').localeCompare(a.checked_at ?? ''));
  return { toBuy, bought };
}
