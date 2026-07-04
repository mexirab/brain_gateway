'use client';

import { ArrowDown, Workflow } from 'lucide-react';
import { Card } from '@/components/ui';
import { useHealth } from '@/lib/hooks';
import { parsePrimaryModel } from '@/lib/model-name';

const TOOLS = [
  'Home Assistant', 'Memory (RAG)', 'Reminders',
  'Focus Timer', 'Web Search', 'Calendar',
  'Email', 'Finance',
];

function FlowBox({ label, sub, color }: { label: string; sub: string; color: string }) {
  return (
    <div className={`px-4 py-3 rounded-xl border-2 ${color} text-center min-w-[140px]`}>
      <div className="font-bold text-sm">{label}</div>
      <div className="text-xs opacity-70 mt-0.5">{sub}</div>
    </div>
  );
}

export default function FlowDiagram() {
  const { data: health } = useHealth();
  const brainModel = parsePrimaryModel(health?.primary);

  // v7 unified flow: User → Frontend → Orchestrator → Brain. No Open WebUI.
  const flowSteps = [
    { label: 'User', sub: 'Voice / Web UI', color: 'bg-warning/20 border-warning/40 text-warning' },
    { label: 'Frontend', sub: 'Next.js · Jupiter', color: 'bg-surface-overlay/40 border-line/40 text-content-primary' },
    { label: 'Orchestrator', sub: 'Unified loop · Jupiter', color: 'bg-brand/20 border-brand/40 text-brand' },
    { label: 'Brain', sub: `${brainModel} · Helios (wake-on-demand)`, color: 'bg-success/20 border-success/40 text-success' },
  ];

  return (
    <Card padding="none" className="p-6 md:p-8">
      <h2 className="text-lg font-bold text-white mb-6 flex items-center gap-2">
        <Workflow size={20} className="text-brand" />
        Data Flow
      </h2>

      {/* Vertical flow: User -> Frontend -> Orchestrator -> Brain */}
      <div className="flex flex-col items-center gap-2 mb-4">
        {flowSteps.map((step, i) => (
          <div key={step.label} className="flex flex-col items-center">
            <FlowBox {...step} />
            {i < flowSteps.length - 1 && (
              <ArrowDown size={20} className="text-content-muted my-1" />
            )}
          </div>
        ))}
      </div>

      {/* Arrow down to tools */}
      <div className="flex justify-center mb-3">
        <ArrowDown size={20} className="text-content-muted" />
      </div>

      {/* Tools grid */}
      <div className="flex justify-center">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
          {TOOLS.map((t) => (
            <div
              key={t}
              className="text-xs px-2.5 py-1.5 rounded-lg bg-surface-raised/70 text-content-secondary border border-line/50 text-center whitespace-nowrap"
            >
              {t}
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}
