'use client';

import { CLUSTER_NODES, CAPABILITIES } from '@/lib/constants';
import { useHealth, useServices } from '@/lib/hooks';
import { nodeStatus } from '@/lib/node-status';
import { parsePrimaryModel } from '@/lib/model-name';
import NodeCard from '@/components/architecture/NodeCard';
import FlowDiagram from '@/components/architecture/FlowDiagram';
import SystemDiagram from '@/components/architecture/SystemDiagram';
import CapabilityCard from '@/components/architecture/CapabilityCard';
import { Server, Cpu, Wrench, FileText } from 'lucide-react';
import { Card } from '@/components/ui';

export default function ArchitecturePage() {
  const { data: health } = useHealth();
  const { data: services } = useServices();

  // 4 real nodes, derived — never hard-code the count.
  const nodeCount = CLUSTER_NODES.length;
  const gpuCount = CLUSTER_NODES.filter((n) => n.gpu).length;

  // Tools + RAG docs come from live /health when reachable, else genericized.
  const toolCount = health?.tools?.length;
  const ragDocs = health?.rag_docs;
  const modelName = parsePrimaryModel(health?.primary);

  const stats = [
    { icon: Server, label: 'Nodes', value: String(nodeCount) },
    { icon: Cpu, label: 'GPUs', value: String(gpuCount) },
    { icon: Wrench, label: 'Tools', value: toolCount != null ? String(toolCount) : '—' },
    { icon: FileText, label: 'RAG Docs', value: ragDocs != null ? String(ragDocs) : '—' },
  ];

  return (
    <main className="min-h-screen p-6 md:p-10">
      <div className="max-w-6xl mx-auto space-y-8">
        {/* Header */}
        <div>
          <h1 className="text-3xl md:text-4xl font-bold mb-2">
            Cluster Architecture
          </h1>
          <p className="text-content-secondary text-sm md:text-base">
            Brain Gateway runs across a {nodeCount}-node home lab cluster with a
            unified AI brain ({modelName}) handling both conversation and tool
            execution. Node status below is live.
          </p>
        </div>

        {/* Stats bar */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {stats.map(({ icon: Icon, label, value }) => (
            <Card key={label} padding="sm" className="flex items-center gap-3">
              <Icon size={20} className="text-brand shrink-0" />
              <div>
                <div className="text-2xl font-bold text-white">{value}</div>
                <div className="text-xs text-content-muted">{label}</div>
              </div>
            </Card>
          ))}
        </div>

        {/* Cluster nodes */}
        <section>
          <h2 className="text-xl font-bold mb-4">Cluster Nodes</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {CLUSTER_NODES.map((node) => (
              <NodeCard
                key={node.id}
                node={node}
                status={nodeStatus(node, services)}
              />
            ))}
          </div>
        </section>

        {/* Interactive system diagram */}
        <section>
          <SystemDiagram />
        </section>

        {/* Simple data flow */}
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
