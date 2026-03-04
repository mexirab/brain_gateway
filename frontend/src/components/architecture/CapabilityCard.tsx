'use client';

import {
  Mic, Timer, Calendar, Home, Brain, Search,
  type LucideIcon,
} from 'lucide-react';

const ICON_MAP: Record<string, LucideIcon> = {
  Mic, Timer, Calendar, Home, Brain, Search,
};

interface CapabilityCardProps {
  icon: string;
  label: string;
  desc: string;
}

export default function CapabilityCard({ icon, label, desc }: CapabilityCardProps) {
  const Icon = ICON_MAP[icon] || Brain;

  return (
    <div className="glass p-4 flex items-center gap-3 hover:border-indigo-500/30 transition-colors">
      <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400 shrink-0">
        <Icon size={20} />
      </div>
      <div>
        <h3 className="text-sm font-semibold text-white">{label}</h3>
        <p className="text-xs text-zinc-500">{desc}</p>
      </div>
    </div>
  );
}
