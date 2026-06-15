'use client';

import {
  Mic, Timer, Calendar, Home, Brain, Search,
  type LucideIcon,
} from 'lucide-react';
import { Card } from '@/components/ui';

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
    <Card padding="sm" className="flex items-center gap-3 hover:border-brand/30 transition-colors">
      <div className="p-2 rounded-lg bg-brand/10 text-brand shrink-0">
        <Icon size={20} />
      </div>
      <div>
        <h3 className="text-sm font-semibold text-white">{label}</h3>
        <p className="text-xs text-content-muted">{desc}</p>
      </div>
    </Card>
  );
}
