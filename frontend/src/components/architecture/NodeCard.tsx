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
  jupiter: 'border-warning/30',
  saturn: 'border-info/30',
  uranus: 'border-accent-violet/30',
  helios: 'border-success/30',
  ha: 'border-accent-cyan/30',
};

const GPU_COLORS: Record<string, string> = {
  jupiter: 'text-warning',
  saturn: 'text-info',
  uranus: 'text-accent-violet',
  helios: 'text-success',
  ha: 'text-accent-cyan',
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
  const borderColor = NODE_COLORS[id] || 'border-line-strong/30';
  const accentColor = GPU_COLORS[id] || 'text-content-secondary';

  return (
    <div className={`glass p-5 ${borderColor} border-2 hover:scale-[1.02] transition-transform`}>
      <div className="flex items-start gap-3 mb-3">
        <div className={`p-2 rounded-lg bg-surface-raised/60 ${accentColor}`}>
          <Icon size={22} />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-lg font-bold text-white">{name}</h3>
          <p className="text-xs text-content-muted font-mono">{ip}</p>
        </div>
      </div>

      <p className="text-sm text-content-primary mb-2">{role}</p>

      {gpu && (
        <div className={`inline-flex items-center gap-1.5 text-xs font-medium ${accentColor} bg-surface-raised/60 px-2 py-1 rounded mb-3`}>
          <Cpu size={12} />
          {gpu}
        </div>
      )}

      <div className="flex flex-wrap gap-1.5 mt-2">
        {services.map((s) => (
          <span
            key={s}
            className="text-xs px-2 py-0.5 rounded-full bg-surface-raised/80 text-content-secondary border border-line/50"
          >
            {s}
          </span>
        ))}
      </div>
    </div>
  );
}
