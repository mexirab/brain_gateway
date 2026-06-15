'use client';

import { useEffect, useState, useCallback } from 'react';
import { Home, RefreshCw } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';
import EntityGroup from '@/components/home/EntityGroup';
import { Card } from '@/components/ui';

const DOMAIN_ORDER = ['light', 'switch', 'fan', 'scene', 'climate', 'cover', 'lock'];

export default function HomePage() {
  const [groups, setGroups] = useState<Record<string, HAEntity[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchEntities = useCallback(() => {
    setLoading(true);
    api
      .entities()
      .then((data) => {
        setGroups(data.controllable);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchEntities();
  }, [fetchEntities]);

  const handleStateChange = (entityId: string, newState: string) => {
    setGroups((prev) => {
      const updated = { ...prev };
      for (const domain of Object.keys(updated)) {
        updated[domain] = updated[domain].map((e) =>
          e.entity_id === entityId ? { ...e, state: newState } : e,
        );
      }
      return updated;
    });
  };

  const sortedDomains = DOMAIN_ORDER.filter((d) => groups[d]?.length > 0);
  // Add any domains not in the predefined order
  Object.keys(groups).forEach((d) => {
    if (!sortedDomains.includes(d) && groups[d].length > 0) {
      sortedDomains.push(d);
    }
  });

  const totalEntities = Object.values(groups).reduce((sum, arr) => sum + arr.length, 0);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Home size={24} className="text-brand" />
          Home Controls
        </h1>
        <div className="flex items-center gap-3">
          {totalEntities > 0 && (
            <span className="text-xs text-content-muted">{totalEntities} entities</span>
          )}
          <button
            onClick={fetchEntities}
            disabled={loading}
            className="p-2 rounded-lg bg-surface-raised/60 text-content-secondary hover:text-white transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {loading && sortedDomains.length === 0 && (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <Card key={i} padding="lg" className="h-32 animate-pulse" />
          ))}
        </div>
      )}

      {error && (
        <Card padding="lg" className="text-center">
          <p className="text-sm text-danger/70">Could not reach Home Assistant</p>
          <p className="text-xs text-content-muted mt-1">{error}</p>
        </Card>
      )}

      {!error && sortedDomains.length > 0 && (
        <div className="space-y-4">
          {sortedDomains.map((domain) => (
            <EntityGroup
              key={domain}
              domain={domain}
              entities={groups[domain]}
              onStateChange={handleStateChange}
            />
          ))}
        </div>
      )}

      {!loading && !error && sortedDomains.length === 0 && (
        <Card padding="lg" className="text-center">
          <p className="text-sm text-content-muted">No controllable entities found</p>
        </Card>
      )}
    </div>
  );
}
