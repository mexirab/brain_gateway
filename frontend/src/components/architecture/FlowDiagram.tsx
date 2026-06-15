'use client';

import { ArrowDown, Workflow } from 'lucide-react';
import { Card } from '@/components/ui';

const FLOW_STEPS = [
  { label: 'User', sub: 'Voice / Web UI', color: 'bg-warning/20 border-warning/40 text-warning' },
  { label: 'Open WebUI', sub: 'HTTPS Gateway', color: 'bg-surface-overlay/40 border-line/40 text-content-primary' },
  { label: 'Orchestrator', sub: 'Mode Router + Intent', color: 'bg-brand/20 border-brand/40 text-brand' },
  { label: 'Brain', sub: 'Qwen3.5-27B · Conversation + Tools', color: 'bg-success/20 border-success/40 text-success' },
];

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
  return (
    <Card padding="none" className="p-6 md:p-8">
      <h2 className="text-lg font-bold text-white mb-6 flex items-center gap-2">
        <Workflow size={20} className="text-brand" />
        Data Flow
      </h2>

      {/* Vertical flow: User -> OpenWebUI -> Orchestrator -> Brain */}
      <div className="flex flex-col items-center gap-2 mb-4">
        {FLOW_STEPS.map((step, i) => (
          <div key={step.label} className="flex flex-col items-center">
            <FlowBox {...step} />
            {i < FLOW_STEPS.length - 1 && (
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
