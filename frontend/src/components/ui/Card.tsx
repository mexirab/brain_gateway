import { ComponentPropsWithoutRef, ElementType } from 'react';

type CardOwnProps<E extends ElementType> = {
  /** Render the card as a different element/component (e.g. "a", "form", or a
   *  Next.js `Link`) instead of the default <div>. Props for that element
   *  (href, onSubmit, …) are accepted and forwarded. */
  as?: E;
  padding?: 'none' | 'sm' | 'md' | 'lg';
  className?: string;
};

type CardProps<E extends ElementType> = CardOwnProps<E> &
  Omit<ComponentPropsWithoutRef<E>, keyof CardOwnProps<E>>;

const PAD: Record<NonNullable<CardOwnProps<ElementType>['padding']>, string> = {
  none: '',
  sm: 'p-4',
  md: 'p-5',
  lg: 'p-6',
};

/** The canonical surface/card. Wraps the `.card` class (alias of `.glass`).
 *  Polymorphic via `as` so the same surface styling backs link-cards
 *  (`as={Link}` / `as="a"`) and form wrappers (`as="form"`). */
export function Card<E extends ElementType = 'div'>({
  as,
  padding = 'md',
  className = '',
  ...props
}: CardProps<E>) {
  const Component = (as || 'div') as ElementType;
  return <Component className={`card ${PAD[padding]} ${className}`.trim()} {...props} />;
}
