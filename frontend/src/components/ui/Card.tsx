import { HTMLAttributes } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  padding?: 'none' | 'sm' | 'md' | 'lg';
}

const PAD: Record<NonNullable<CardProps['padding']>, string> = {
  none: '',
  sm: 'p-4',
  md: 'p-5',
  lg: 'p-6',
};

/** The canonical surface/card. Wraps the `.card` class (alias of `.glass`). */
export function Card({ padding = 'md', className = '', ...props }: CardProps) {
  return <div className={`card ${PAD[padding]} ${className}`.trim()} {...props} />;
}
