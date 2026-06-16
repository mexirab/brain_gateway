import { ButtonHTMLAttributes, forwardRef } from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success';
type Size = 'sm' | 'md';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  /** Square, padding-only button sized for a single icon child. Always pair
   *  with an `aria-label` (or `title`) so the control is named for screen
   *  readers — an icon alone has no accessible name. */
  icon?: boolean;
}

const VARIANTS: Record<Variant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  danger: 'btn-danger',
  success: 'btn-success',
};

const SIZES: Record<Size, string> = { sm: 'btn-sm', md: 'btn-md' };
const ICON_SIZES: Record<Size, string> = { sm: 'btn-icon-sm', md: 'btn-icon-md' };

/** Single source of truth for buttons. Replaces the ad-hoc inline button
 *  styling scattered across pages. Variants/sizes map to the .btn-* classes.
 *  Set `icon` for square icon-only buttons (trash/pencil/close/chevron). */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = 'primary', size = 'md', icon = false, className = '', ...props }, ref) => (
    <button
      ref={ref}
      className={`btn ${(icon ? ICON_SIZES : SIZES)[size]} ${VARIANTS[variant]} ${className}`.trim()}
      {...props}
    />
  ),
);
Button.displayName = 'Button';
