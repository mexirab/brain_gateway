'use client';

import { Thermometer, TrendingUp, TrendingDown, AlertTriangle } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { useTemperatures } from '@/lib/hooks';
import { barGeometry, DEFAULT_THRESHOLDS, tempBand, type Band } from '@/lib/temp-band';

const TEXT_COLOR: Record<Band, string> = {
  unknown: 'text-content-secondary',
  ok: 'text-success',
  warning: 'text-warning',
  critical: 'text-danger',
};
const BAR_COLOR: Record<Band, string> = {
  unknown: 'bg-surface-overlay',
  ok: 'bg-success',
  warning: 'bg-warning',
  critical: 'bg-danger',
};

export default function TemperatureCard() {
  const { data, error, isLoading, mutate } = useTemperatures();

  const closetTemp = data?.sensors?.closet?.temperature;
  // The orchestrator calls the room sensor `ambient`; older builds said `kitchen`.
  const ambientTemp = (data?.sensors?.ambient ?? data?.sensors?.kitchen)?.temperature;
  const delta = data?.delta;
  const { warning, critical } = data?.thresholds ?? DEFAULT_THRESHOLDS;
  const band = tempBand(closetTemp, warning, critical);

  const getDeltaColor = (d: number | null | undefined) => {
    if (d == null) return 'text-content-secondary';
    if (d >= 8) return 'text-danger';
    if (d >= 3) return 'text-warning';
    return 'text-success';
  };

  const bar = barGeometry(closetTemp, warning, critical);
  const alerting = band === 'warning' || band === 'critical';

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Thermometer size={18} className="text-warning" />
        Server Closet
        {alerting && (
          <AlertTriangle
            size={16}
            role="img"
            aria-label={band === 'critical' ? 'Critical temperature' : 'High temperature'}
            className={`${band === 'critical' ? 'text-danger' : 'text-warning'} motion-safe:animate-pulse`}
          />
        )}
      </h2>

      {isLoading && <Skeleton className="h-32" />}
      {!isLoading && error && (
        <ErrorState compact message="Sensors unavailable" onRetry={() => mutate()} />
      )}

      {!isLoading && !error && data && (
        <div className="space-y-3">
          {/* Main closet temperature */}
          <div className="flex items-baseline justify-between">
            <span className="flex items-baseline gap-2">
              <span className={`text-3xl font-bold tabular-nums ${TEXT_COLOR[band]}`}>
                {closetTemp != null ? `${closetTemp.toFixed(1)}°` : '--'}
              </span>
              {alerting && (
                <span className={`text-xs font-semibold uppercase ${TEXT_COLOR[band]}`}>
                  {band === 'critical' ? 'Critical' : 'Warning'}
                </span>
              )}
            </span>
            <span className="text-xs text-content-muted uppercase tracking-wider">Server Closet</span>
          </div>

          {/* Temperature bar, with ticks at the warning and critical thresholds */}
          <div
            className="relative w-full bg-surface-raised rounded-full h-2"
            role="meter"
            aria-label="Server closet temperature"
            aria-valuemin={bar.min}
            aria-valuemax={bar.max}
            aria-valuenow={closetTemp ?? undefined}
            aria-valuetext={closetTemp != null ? `${closetTemp.toFixed(1)} degrees` : 'No reading'}
          >
            <div
              className={`h-2 rounded-full transition-all duration-1000 ${BAR_COLOR[band]}`}
              style={{ width: `${bar.progress}%` }}
            />
            <span
              aria-hidden
              className="absolute -top-0.5 h-3 w-0.5 bg-warning"
              style={{ left: `${bar.warningPct}%` }}
            />
            <span
              aria-hidden
              className="absolute -top-0.5 h-3 w-0.5 bg-danger"
              style={{ left: `${bar.criticalPct}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-content-muted">
            <span>{bar.min}°</span>
            <span>
              warns at {warning}° · critical at {critical}°
            </span>
            <span>{bar.max}°</span>
          </div>

          {/* Ambient + Delta */}
          <div className="pt-2 border-t border-line-subtle space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-content-secondary">Room (ambient)</span>
              <span className="text-content-primary tabular-nums">
                {ambientTemp != null ? `${ambientTemp.toFixed(1)}°F` : '--'}
              </span>
            </div>

            <div className="flex items-center justify-between text-sm">
              <span className="text-content-secondary flex items-center gap-1">
                {delta != null && delta > 0 ? (
                  <TrendingUp size={14} className={getDeltaColor(delta)} />
                ) : (
                  <TrendingDown size={14} className="text-success" />
                )}
                Heat delta
              </span>
              <span className={`font-medium tabular-nums ${getDeltaColor(delta)}`}>
                {delta != null ? `${delta > 0 ? '+' : ''}${delta.toFixed(1)}°F` : '--'}
              </span>
            </div>
          </div>

          {/* Cost estimate */}
          {data.estimated_monthly_cooling_cost != null && data.estimated_monthly_cooling_cost > 0 && (
            <div className="pt-2 border-t border-line-subtle flex items-center justify-between text-xs text-content-muted">
              <span>Est. cooling cost</span>
              <span className="text-content-secondary">${data.estimated_monthly_cooling_cost.toFixed(2)}/mo</span>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
