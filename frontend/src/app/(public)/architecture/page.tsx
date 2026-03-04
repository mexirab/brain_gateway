import { CLUSTER_NODES, CAPABILITIES } from '@/lib/constants';
import NodeCard from '@/components/architecture/NodeCard';
import FlowDiagram from '@/components/architecture/FlowDiagram';
import CapabilityCard from '@/components/architecture/CapabilityCard';
import { Server, Cpu, Wrench, FileText } from 'lucide-react';

const STATS = [
  { icon: Server, label: 'Nodes', value: '5' },
  { icon: Cpu, label: 'GPUs', value: '5' },
  { icon: Wrench, label: 'Tools', value: '14' },
  { icon: FileText, label: 'RAG Docs', value: '154' },
];

export default function ArchitecturePage() {
  return (
    <main className="min-h-screen p-6 md:p-10">
      <div className="max-w-6xl mx-auto space-y-8">
        {/* Header */}
        <div>
          <h1 className="text-3xl md:text-4xl font-bold mb-2">
            Cluster Architecture
          </h1>
          <p className="text-zinc-400 text-sm md:text-base">
            Brain Gateway runs across a 5-node home lab cluster with dedicated GPUs for inference, TTS, and STT.
          </p>
        </div>

        {/* Stats bar */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {STATS.map(({ icon: Icon, label, value }) => (
            <div key={label} className="glass p-4 flex items-center gap-3">
              <Icon size={20} className="text-indigo-400 shrink-0" />
              <div>
                <div className="text-2xl font-bold text-white">{value}</div>
                <div className="text-xs text-zinc-500">{label}</div>
              </div>
            </div>
          ))}
        </div>

        {/* Cluster nodes */}
        <section>
          <h2 className="text-xl font-bold mb-4">Cluster Nodes</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {CLUSTER_NODES.map((node) => (
              <NodeCard key={node.id} {...node} />
            ))}
          </div>
        </section>

        {/* Data flow */}
        <section>
          <FlowDiagram />
        </section>

        {/* Capabilities */}
        <section>
          <h2 className="text-xl font-bold mb-4">Capabilities</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {CAPABILITIES.map((cap) => (
              <CapabilityCard key={cap.label} {...cap} />
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}
