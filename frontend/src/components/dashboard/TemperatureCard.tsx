'use client';

import { useEffect, useState } from 'react';
import { Thermometer, TrendingUp, TrendingDown, AlertTriangle } from 'lucide-react';
import { api } from '@/lib/api';
import type { TemperaturesResponse } from '@/lib/types';

export default function TemperatureCard() {
  const [data, setData] = useState<TemperaturesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchTemps = () => {
    api
      .temperatures()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchTemps();
    const interval = setInterval(fetchTemps, 60_000); // Poll every 60s
    return () => clearInterval(interval);
  }, []);

  const closetTemp = data?.sensors?.closet?.temperature;
  const kitchenTemp = data?.sensors?.kitchen?.temperature;
  const delta = data?.delta;

  // Color coding for closet temp
  const getTempColor = (temp: number | null | undefined) => {
    if (temp == null) return 'text-zinc-400';
    if (temp >= 85) return 'text-red-400';
    if (temp >= 80) return 'text-amber-400';
    if (temp >= 75) return 'text-yellow-400';
    return 'text-emerald-400';
  };

  const getDeltaColor = (d: number | null | undefined) => {
    if (d == null) return 'text-zinc-400';
    if (d >= 8) return 'text-red-400';
    if (d >= 5) return 'text-amber-400';
    if (d >= 3) return 'text-yellow-400';
    return 'text-emerald-400';
  };

  // Progress bar for closet temp (65-90 range)
  const tempProgress = closetTemp ? Math.min(100, Math.max(0, ((closetTemp - 65) / 25) * 100)) : 0;
  const getBarColor = (temp: number | null | undefined) => {
    if (temp == null) return 'bg-zinc-600';
    if (temp >= 85) return 'bg-red-500';
    if (temp >= 80) return 'bg-amber-500';
    if (temp >= 75) return 'bg-yellow-500';
    return 'bg-emerald-500';
  };

  return (
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Thermometer size={18} className="text-orange-400" />
        Server Closet
        {closetTemp != null && closetTemp >= 80 && (
          <AlertTriangle size={16} className="text-amber-400 animate-pulse" />
        )}
      </h2>

      {loading && <div className="h-32 bg-zinc-800/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-red-400/70">Sensors unavailable</p>}

      {!loading && !error && data && (
        <div className="space-y-3">
          {/* Main closet temperature */}
          <div className="flex items-baseline justify-between">
            <span className={`text-3xl font-bold tabular-nums ${getTempColor(closetTemp)}`}>
              {closetTemp != null ? `${closetTemp.toFixed(1)}°` : '--'}
            </span>
            <span className="text-xs text-zinc-500 uppercase tracking-wider">Server Closet</span>
          </div>

          {/* Temperature bar */}
          <div className="w-full bg-zinc-800 rounded-full h-2">
            <div
              className={`h-2 rounded-full transition-all duration-1000 ${getBarColor(closetTemp)}`}
              style={{ width: `${tempProgress}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-zinc-600">
            <span>65°</span>
            <span>75°</span>
            <span>85°</span>
            <span>90°</span>
          </div>

          {/* Kitchen + Delta */}
          <div className="pt-2 border-t border-zinc-800 space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-zinc-400">Kitchen (ambient)</span>
              <span className="text-zinc-300 tabular-nums">
                {kitchenTemp != null ? `${kitchenTemp.toFixed(1)}°F` : '--'}
              </span>
            </div>

            <div className="flex items-center justify-between text-sm">
              <span className="text-zinc-400 flex items-center gap-1">
                {delta != null && delta > 0 ? (
                  <TrendingUp size={14} className={getDeltaColor(delta)} />
                ) : (
                  <TrendingDown size={14} className="text-emerald-400" />
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
            <div className="pt-2 border-t border-zinc-800 flex items-center justify-between text-xs text-zinc-500">
              <span>Est. cooling cost</span>
              <span className="text-zinc-400">${data.estimated_monthly_cooling_cost.toFixed(2)}/mo</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
