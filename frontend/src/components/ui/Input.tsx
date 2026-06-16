import { InputHTMLAttributes, forwardRef } from 'react';

/** Canonical text input. Wraps the `.input` class so form fields match the
 *  design system without re-typing the className on every <input>. Forwards a
 *  ref (autofocus, focus traps) and all native input props. */
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className = '', ...props }, ref) => (
    <input ref={ref} className={`input ${className}`.trim()} {...props} />
  ),
);
Input.displayName = 'Input';
