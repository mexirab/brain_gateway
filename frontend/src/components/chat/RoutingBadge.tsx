'use client';

import type { RoutingInfo } from '@/lib/types';

const MODE_COLORS: Record<string, string> = {
  explainer: 'bg-blue-500/20 text-blue-400',
  mirror: 'bg-purple-500/20 text-purple-400',
  counterbalance: 'bg-amber-500/20 text-amber-400',
  challenge: 'bg-red-500/20 text-red-400',
  baseline: 'bg-emerald-500/20 text-emerald-400',
};

const INTENSITY_COLORS: Record<string, string> = {
  low: 'text-zinc-500',
  medium: 'text-amber-500',
  high: 'text-red-500',
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
