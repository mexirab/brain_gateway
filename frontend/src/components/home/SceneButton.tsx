'use client';

import { useState } from 'react';
import { Sparkles } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';

interface SceneButtonProps {
  entity: HAEntity;
}

export default function SceneButton({ entity }: SceneButtonProps) {
  const [activated, setActivated] = useState(false);
  const [acting, setActing] = useState(false);

  const handleActivate = async () => {
    setActing(true);
    try {
      await api.haCommand(entity.entity_id, 'turn_on');
      setActivated(true);
      setTimeout(() => setActivated(false), 2000);
    } catch {
      // silently fail
    } finally {
      setActing(false);
    }
  };

  return (
    <button
      onClick={handleActivate}
      disabled={acting}
      className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm transition-all ${
        activated
          ? 'bg-indigo-500/30 text-indigo-300 border border-indigo-500/40'
          : 'bg-zinc-800/40 text-zinc-400 border border-zinc-700/30 hover:text-white hover:border-zinc-600/50'
      } disabled:opacity-50`}
    >
      <Sparkles size={14} />
      {entity.friendly_name}
    </button>
  );
}
