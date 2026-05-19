// Shared selfcare-category constants — used by both the Settings panel
// (`components/settings/SelfcarePanel.tsx`) and the setup wizard's Selfcare
// step (`components/setup/SelfcareStep.tsx`) so the two can't drift.

/** Known categories, in the order they should be displayed. */
export const CATEGORY_ORDER = ['meds', 'meals', 'water', 'movement'];

/** Human-readable labels. Unknown categories fall back to their raw key. */
export const CATEGORY_LABELS: Record<string, string> = {
  meds: 'Medication',
  meals: 'Meals',
  water: 'Water / Hydration',
  movement: 'Movement / Posture',
};

/** Known categories first (in CATEGORY_ORDER), then any extras alphabetically. */
export function orderedCategoryNames(
  categories: Record<string, unknown>,
): string[] {
  const known = CATEGORY_ORDER.filter((n) => n in categories);
  const extras = Object.keys(categories)
    .filter((n) => !CATEGORY_ORDER.includes(n))
    .sort();
  return [...known, ...extras];
}
