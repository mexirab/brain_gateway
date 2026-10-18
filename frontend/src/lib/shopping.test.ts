import { describe, expect, it } from 'vitest';
import { splitShopping } from './shopping';
import type { ShoppingItem } from './types';

const item = (id: number, checked: number, checked_at: string | null = null): ShoppingItem => ({
  id,
  item: `item ${id}`,
  list_name: 'grocery',
  checked,
  added_at: '2026-10-18T10:00:00',
  checked_at,
});

describe('splitShopping', () => {
  it('keeps unbought items in API order', () => {
    const { toBuy } = splitShopping([item(3, 0), item(1, 0), item(2, 1, '2026-10-18T11:00:00')]);
    expect(toBuy.map((i) => i.id)).toEqual([3, 1]);
  });

  it('puts the most recently bought first', () => {
    const { bought } = splitShopping([
      item(1, 1, '2026-10-18T11:00:00'),
      item(2, 1, '2026-10-18T11:05:00'),
      item(3, 1, null),
      item(4, 0),
    ]);
    expect(bought.map((i) => i.id)).toEqual([2, 1, 3]);
  });
});
