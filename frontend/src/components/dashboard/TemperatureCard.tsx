'use client';

import { Thermometer, TrendingUp, TrendingDown, AlertTriangle } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { useTemperatures } from '@/lib/hooks';

export default function TemperatureCard() {
  const { data, error, isLoading, mutate } = useTemperatures();

  const closetTemp = data?.sensors?.closet?.temperature;
  const kitchenTemp = data?.sensors?.kitchen?.temperature;
  const delta = data?.delta;

  // Color coding for closet temp
  const getTempColor = (temp: number | null | undefined) => {
    if (temp == null) return 'text-content-secondary';
    if (temp >= 85) return 'text-danger';
    if (temp >= 80) return 'text-warning';
    if (temp >= 75) return 'text-warning';
    return 'text-success';
  };

  const getDeltaColor = (d: number | null | undefined) => {
    if (d == null) return 'text-content-secondary';
    if (d >= 8) return 'text-danger';
    if (d >= 5) return 'text-warning';
    if (d >= 3) return 'text-warning';
    return 'text-success';
  };

  // Progress bar for closet temp (65-90 range)
  const tempProgress = closetTemp ? Math.min(100, Math.max(0, ((closetTemp - 65) / 25) * 100)) : 0;
  const getBarColor = (temp: number | null | undefined) => {
    if (temp == null) return 'bg-surface-overlay';
    if (temp >= 85) return 'bg-danger';
    if (temp >= 80) return 'bg-warning';
    if (temp >= 75) return 'bg-warning';
    return 'bg-success';
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Thermometer size={18} className="text-warning" />
        Server Closet
        {closetTemp != null && closetTemp >= 80 && (
          <AlertTriangle size={16} className="text-warning animate-pulse" />
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
            <span className={`text-3xl font-bold tabular-nums ${getTempColor(closetTemp)}`}>
              {closetTemp != null ? `${closetTemp.toFixed(1)}°` : '--'}
            </span>
            <span className="text-xs text-content-muted uppercase tracking-wider">Server Closet</span>
          </div>

          {/* Temperature bar */}
          <div className="w-full bg-surface-raised rounded-full h-2">
            <div
              className={`h-2 rounded-full transition-all duration-1000 ${getBarColor(closetTemp)}`}
              style={{ width: `${tempProgress}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-content-muted">
            <span>65°</span>
            <span>75°</span>
            <span>85°</span>
            <span>90°</span>
          </div>

          {/* Kitchen + Delta */}
          <div className="pt-2 border-t border-line-subtle space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-content-secondary">Kitchen (ambient)</span>
              <span className="text-content-primary tabular-nums">
                {kitchenTemp != null ? `${kitchenTemp.toFixed(1)}°F` : '--'}
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
                {delta != null ? `+${delta.toFixed(1)}°F` : '--'}
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
