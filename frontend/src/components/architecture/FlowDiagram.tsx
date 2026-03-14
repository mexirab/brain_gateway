'use client';

import { ArrowDown, Workflow } from 'lucide-react';

const FLOW_STEPS = [
  { label: 'User', sub: 'Voice / Web UI', color: 'bg-amber-500/20 border-amber-500/40 text-amber-300' },
  { label: 'Open WebUI', sub: 'HTTPS Gateway', color: 'bg-zinc-700/40 border-zinc-600/40 text-zinc-300' },
  { label: 'Orchestrator', sub: 'Mode Router + Intent', color: 'bg-indigo-500/20 border-indigo-500/40 text-indigo-300' },
  { label: 'Brain', sub: 'Qwen3.5-27B · Conversation + Tools', color: 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300' },
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
    <div className="glass p-6 md:p-8">
      <h2 className="text-lg font-bold text-white mb-6 flex items-center gap-2">
        <Workflow size={20} className="text-indigo-400" />
        Data Flow
      </h2>

      {/* Vertical flow: User -> OpenWebUI -> Orchestrator -> Brain */}
      <div className="flex flex-col items-center gap-2 mb-4">
        {FLOW_STEPS.map((step, i) => (
          <div key={step.label} className="flex flex-col items-center">
            <FlowBox {...step} />
            {i < FLOW_STEPS.length - 1 && (
              <ArrowDown size={20} className="text-zinc-600 my-1" />
            )}
          </div>
        ))}
      </div>

      {/* Arrow down to tools */}
      <div className="flex justify-center mb-3">
        <ArrowDown size={20} className="text-zinc-600" />
      </div>

      {/* Tools grid */}
      <div className="flex justify-center">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
          {TOOLS.map((t) => (
            <div
              key={t}
              className="text-xs px-2.5 py-1.5 rounded-lg bg-zinc-800/70 text-zinc-400 border border-zinc-700/50 text-center whitespace-nowrap"
            >
              {t}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
