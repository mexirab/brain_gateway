'use client';

import type { RoutingInfo } from '@/lib/types';

const MODE_COLORS: Record<string, string> = {
  explainer: 'bg-info/20 text-info',
  mirror: 'bg-brand/20 text-brand',
  counterbalance: 'bg-warning/20 text-warning',
  challenge: 'bg-danger/20 text-danger',
  baseline: 'bg-success/20 text-success',
};

const INTENSITY_COLORS: Record<string, string> = {
  low: 'text-content-muted',
  medium: 'text-warning',
  high: 'text-danger',
};

interface RoutingBadgeProps {
  routing: RoutingInfo;
}

export default function RoutingBadge({ routing }: RoutingBadgeProps) {
  const modeColor = MODE_COLORS[routing.intent_mode] || MODE_COLORS.explainer;
  const intensityColor = INTENSITY_COLORS[routing.intent_intensity] || INTENSITY_COLORS.low;

  return (
    <div className="flex items-center gap-1.5 mt-1">
      <span className={`text-xs px-2 py-0.5 rounded-full ${modeColor}`}>
        {routing.intent_mode}
      </span>
      <span className={`text-xs ${intensityColor}`}>
        {routing.intent_intensity}
      </span>
    </div>
  );
}
