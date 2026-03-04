'use client';

import { ArrowDown, ArrowRight, GitBranch } from 'lucide-react';

const FLOW_STEPS = [
  { label: 'User', sub: 'Voice / Web UI', color: 'bg-amber-500/20 border-amber-500/40 text-amber-300' },
  { label: 'Open WebUI', sub: 'HTTPS Gateway', color: 'bg-zinc-700/40 border-zinc-600/40 text-zinc-300' },
  { label: 'Orchestrator', sub: 'Mode Router + Intent', color: 'bg-indigo-500/20 border-indigo-500/40 text-indigo-300' },
];

const BRANCH_LEFT = { label: 'Helios', sub: 'Conversational AI', color: 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300' };
const BRANCH_RIGHT = { label: 'Nemotron', sub: 'Tool Orchestrator', color: 'bg-blue-500/20 border-blue-500/40 text-blue-300' };

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
        <GitBranch size={20} className="text-indigo-400" />
        Data Flow
      </h2>

      {/* Vertical flow: User -> OpenWebUI -> Orchestrator */}
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

      {/* Branch: Helios (left) / Nemotron (right) */}
      <div className="flex items-center justify-center gap-2 text-zinc-600 mb-2">
        <div className="w-16 h-px bg-zinc-700" />
        <ArrowDown size={16} />
        <div className="w-16 h-px bg-zinc-700" />
      </div>

      <div className="flex flex-col md:flex-row items-center md:items-start justify-center gap-4 md:gap-8 mb-4">
        <div className="flex flex-col items-center gap-2">
          <FlowBox {...BRANCH_LEFT} />
          <span className="text-xs text-zinc-500">Direct response</span>
        </div>

        <div className="hidden md:flex items-center text-zinc-600 mt-4">
          <span className="text-xs text-zinc-500 mr-2">or</span>
          <ArrowRight size={16} />
        </div>
        <div className="md:hidden text-xs text-zinc-500">or</div>

        <div className="flex flex-col items-center gap-2">
          <FlowBox {...BRANCH_RIGHT} />
          <ArrowDown size={16} className="text-zinc-600" />

          {/* Tools grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5 mt-1">
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
    </div>
  );
}
