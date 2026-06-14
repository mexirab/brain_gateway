import { HTMLAttributes } from 'react';

type Tone = 'neutral' | 'brand' | 'success' | 'warning' | 'danger' | 'info';

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

const TONES: Record<Tone, string> = {
  neutral: 'badge-neutral',
  brand: 'badge-brand',
  success: 'badge-success',
  warning: 'badge-warning',
  danger: 'badge-danger',
  info: 'badge-info',
};

/** Status/category pill. Replaces the per-page color maps (announcements,
 *  documents, finance) — pick a semantic tone instead of a raw color. */
export function Badge({ tone = 'neutral', className = '', ...props }: BadgeProps) {
  return <span className={`badge ${TONES[tone]} ${className}`.trim()} {...props} />;
}
