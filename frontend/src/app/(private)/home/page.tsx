'use client';

import { useEffect, useState, useCallback } from 'react';
import { Home, RefreshCw } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';
import EntityGroup from '@/components/home/EntityGroup';
import { Card, Button, ErrorState, EmptyState } from '@/components/ui';

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
      .catch(() => setError('unreachable'))
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
          <Button
            icon
            variant="secondary"
            onClick={fetchEntities}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh entities"
          >
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </Button>
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
        <Card padding="lg">
          <ErrorState
            title="Could not reach Home Assistant"
            message="We couldn’t load your devices. Check that Home Assistant is reachable, then try again."
            onRetry={fetchEntities}
          />
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
        <Card padding="lg">
          <EmptyState
            icon={<Home size={40} />}
            title="No controllable entities found"
            description="Once Home Assistant exposes devices, they’ll show up here."
          />
        </Card>
      )}
    </div>
  );
}
