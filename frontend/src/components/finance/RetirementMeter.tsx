'use client';

import type { FinanceConfig } from '@/lib/finance-types';
import { formatCurrency, projectedRetirement } from '@/lib/finance-utils';

interface RetirementMeterProps {
  config: FinanceConfig;
}

export default function RetirementMeter({ config }: RetirementMeterProps) {
  const projected = projectedRetirement(config);
  const yearsLeft = config.retirement_target_age - config.current_age;
  const progress = Math.min(100, (config.retirement_current / projected) * 100);

  // Gauge: a top semicircle (opens downward like a speedometer)
  // Circle center at bottom of the viewBox, only top half visible
  const width = 160;
  const strokeWidth = 10;
  const radius = (width - strokeWidth) / 2; // 75
  const cx = width / 2;                     // 80
  const cy = radius + strokeWidth / 2;      // 80 (center = bottom of visible area)
  const svgHeight = cy + strokeWidth / 2;   // 85

  // Left and right endpoints sit at y=cy (the center line)
  const leftX = cx - radius;
  const rightX = cx + radius;

  // Background arc: full semicircle from left to right, sweeping upward
  const bgArc = `M ${leftX} ${cy} A ${radius} ${radius} 0 0 1 ${rightX} ${cy}`;

  // Progress arc: partial sweep from left
  // At 0% we're at the left point, at 100% we reach the right point
  // Angle goes from PI to 0 as progress goes from 0% to 100%
  const angle = Math.PI * (1 - progress / 100);
  const endX = cx + radius * Math.cos(angle);
  const endY = cy - radius * Math.sin(angle);
  const largeArc = progress > 50 ? 1 : 0;
  const progressArc = progress > 0
    ? `M ${leftX} ${cy} A ${radius} ${radius} 0 ${largeArc} 1 ${endX} ${endY}`
    : '';

  return (
    <div className="glass p-5 flex flex-col items-center">
      <h3 className="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-3">
        Retirement Journey
      </h3>

      <svg
        width={width}
        height={svgHeight}
        viewBox={`0 0 ${width} ${svgHeight}`}
        className="mb-1"
      >
        <defs>
          <linearGradient id="retirementGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#6366f1" />
            <stop offset="100%" stopColor="#a855f7" />
          </linearGradient>
        </defs>

        {/* Background arc */}
        <path
          d={bgArc}
          fill="none"
          stroke="#27272a"
          strokeWidth={strokeWidth}
          strokeLinecap="round"
        />

        {/* Progress arc */}
        {progressArc && (
          <path
            d={progressArc}
            fill="none"
            stroke="url(#retirementGradient)"
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            className="transition-all duration-1000 ease-out"
          />
        )}
      </svg>

      {/* Stats below the arc */}
      <p className="text-lg font-bold text-zinc-100">{formatCurrency(config.retirement_current)}</p>
      <p className="text-xs text-zinc-500">{yearsLeft} years to go</p>
      <p className="text-xs text-zinc-400 mt-2">
        Projected at {config.retirement_target_age}: {formatCurrency(projected)}
      </p>
    </div>
  );
}
