import { describe, expect, it } from 'vitest';
import { JUST_BOUGHT, splitShopping } from './shopping';
import type { ShoppingItem } from './types';

const NOW = new Date('2026-10-18T12:00:00');

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
    const { toBuy } = splitShopping([item(3, 0), item(1, 0), item(2, 1, '2026-10-18T11:00:00')], NOW);
    expect(toBuy.map((i) => i.id)).toEqual([3, 1]);
  });

  it('puts the most recently bought first', () => {
    const { bought } = splitShopping(
      [item(1, 1, '2026-10-18T11:00:00'), item(2, 1, '2026-10-18T11:05:00'), item(3, 1, null), item(4, 0)],
      NOW,
    );
    expect(bought.map((i) => i.id)).toEqual([2, 1, 3]);
  });

  it('puts a just-ticked item (before the server answers) at the very top', () => {
    const { bought } = splitShopping([item(1, 1, '2026-10-18T11:59:00'), item(2, 1, JUST_BOUGHT)], NOW);
    expect(bought.map((i) => i.id)).toEqual([2, 1]);
  });

  it('hides items bought more than a week ago', () => {
    const { bought } = splitShopping(
      [item(1, 1, '2026-04-20T09:00:00'), item(2, 1, '2026-10-12T09:00:00'), item(3, 1, '2026-10-17T09:00:00')],
      NOW,
    );
    expect(bought.map((i) => i.id)).toEqual([3, 2]);
  });
});
