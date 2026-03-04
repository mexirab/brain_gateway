'use client';

import { Server, Cpu, Monitor, Home, Mic } from 'lucide-react';

const NODE_ICONS: Record<string, React.ElementType> = {
  jupiter: Server,
  saturn: Cpu,
  uranus: Mic,
  helios: Monitor,
  ha: Home,
};

const NODE_COLORS: Record<string, string> = {
  jupiter: 'border-amber-500/30',
  saturn: 'border-blue-500/30',
  uranus: 'border-purple-500/30',
  helios: 'border-emerald-500/30',
  ha: 'border-cyan-500/30',
};

const GPU_COLORS: Record<string, string> = {
  jupiter: 'text-amber-400',
  saturn: 'text-blue-400',
  uranus: 'text-purple-400',
  helios: 'text-emerald-400',
  ha: 'text-cyan-400',
};

interface NodeCardProps {
  id: string;
  name: string;
  ip: string;
  gpu: string | null;
  role: string;
  services: readonly string[];
}

export default function NodeCard({ id, name, ip, gpu, role, services }: NodeCardProps) {
  const Icon = NODE_ICONS[id] || Server;
  const borderColor = NODE_COLORS[id] || 'border-zinc-500/30';
  const accentColor = GPU_COLORS[id] || 'text-zinc-400';

  return (
    <div className={`glass p-5 ${borderColor} border-2 hover:scale-[1.02] transition-transform`}>
      <div className="flex items-start gap-3 mb-3">
        <div className={`p-2 rounded-lg bg-zinc-800/60 ${accentColor}`}>
          <Icon size={22} />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-lg font-bold text-white">{name}</h3>
          <p className="text-xs text-zinc-500 font-mono">{ip}</p>
        </div>
      </div>

      <p className="text-sm text-zinc-300 mb-2">{role}</p>

      {gpu && (
        <div className={`inline-flex items-center gap-1.5 text-xs font-medium ${accentColor} bg-zinc-800/60 px-2 py-1 rounded mb-3`}>
          <Cpu size={12} />
          {gpu}
        </div>
      )}

      <div className="flex flex-wrap gap-1.5 mt-2">
        {services.map((s) => (
          <span
            key={s}
            className="text-xs px-2 py-0.5 rounded-full bg-zinc-800/80 text-zinc-400 border border-zinc-700/50"
          >
            {s}
          </span>
        ))}
      </div>
    </div>
  );
}
