import { ButtonHTMLAttributes, forwardRef } from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';
type Size = 'sm' | 'md';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

const VARIANTS: Record<Variant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  danger: 'btn-danger',
};

const SIZES: Record<Size, string> = { sm: 'btn-sm', md: 'btn-md' };

/** Single source of truth for buttons. Replaces the ad-hoc inline button
 *  styling scattered across pages. Variants/sizes map to the .btn-* classes. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = 'primary', size = 'md', className = '', ...props }, ref) => (
    <button
      ref={ref}
      className={`btn ${SIZES[size]} ${VARIANTS[variant]} ${className}`.trim()}
      {...props}
    />
  ),
);
Button.displayName = 'Button';
