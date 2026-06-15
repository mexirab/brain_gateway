'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui';

// ── Types ────────────────────────────────────────────────────────────────────

type NodeId =
  | 'user' | 'echo' | 'wake' | 'stt' | 'webui'
  | 'orchestrator' | 'router' | 'brain'
  | 'ha' | 'memory' | 'calendar' | 'email' | 'web' | 'focus' | 'finance'
  | 'tts' | 'speakers'
  | 'bg_calendar' | 'bg_email' | 'bg_morning' | 'bg_travel';

interface DiagramNode {
  id: NodeId;
  label: string;
  sub: string;
  x: number;
  y: number;
  w: number;
  h: number;
  color: string;
  glow: string;
  icon: string;
  group?: string;
}

interface FlowEdge {
  from: NodeId;
  to: NodeId;
  label?: string;
  color?: string;
}

interface FlowPath {
  name: string;
  description: string;
  edges: FlowEdge[];
  color: string;
}

// ── Node definitions ─────────────────────────────────────────────────────────

const NODES: DiagramNode[] = [
  // Input layer
  { id: 'user', label: 'You', sub: 'Voice or Text', x: 330, y: 30, w: 120, h: 56, color: '#fbbf24', glow: '#fbbf24', icon: '🎤', group: 'input' },
  { id: 'echo', label: 'ATOM Echo', sub: 'Wake Word', x: 140, y: 115, w: 130, h: 52, color: '#fbbf24', glow: '#fbbf24', icon: '📡', group: 'input' },
  { id: 'webui', label: 'Open WebUI', sub: 'HTTPS Gateway', x: 510, y: 115, w: 140, h: 52, color: '#84838f', glow: '#84838f', icon: '🌐', group: 'input' },

  // Processing layer
  { id: 'stt', label: 'Whisper STT', sub: 'Speech → Text', x: 140, y: 205, w: 130, h: 52, color: '#a78bfa', glow: '#a78bfa', icon: '👂', group: 'processing' },
  { id: 'orchestrator', label: 'Orchestrator', sub: 'Brain Gateway', x: 320, y: 205, w: 160, h: 60, color: '#6f63ff', glow: '#6f63ff', icon: '🧠', group: 'processing' },
  { id: 'router', label: 'Mode Router', sub: 'Intent + Emotion', x: 520, y: 205, w: 140, h: 52, color: '#6f63ff', glow: '#6f63ff', icon: '🎯', group: 'processing' },

  // AI layer (unified v7 — single brain model)
  { id: 'brain', label: 'Brain', sub: 'Qwen3.5-27B · RTX 5090', x: 320, y: 320, w: 160, h: 56, color: '#34d399', glow: '#34d399', icon: '🧠', group: 'ai' },

  // Tools layer (2 rows of 4 to fit width)
  { id: 'ha', label: 'Home', sub: 'Lights · Switches', x: 50, y: 425, w: 105, h: 46, color: '#34d3e0', glow: '#34d3e0', icon: '🏠', group: 'tools' },
  { id: 'memory', label: 'Memory', sub: '154 RAG Docs', x: 168, y: 425, w: 105, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '🧩', group: 'tools' },
  { id: 'calendar', label: 'Calendar', sub: 'Google Cal', x: 286, y: 425, w: 105, h: 46, color: '#fb7185', glow: '#fb7185', icon: '📅', group: 'tools' },
  { id: 'email', label: 'Email', sub: 'Gmail', x: 404, y: 425, w: 105, h: 46, color: '#fb7185', glow: '#fb7185', icon: '📧', group: 'tools' },
  { id: 'web', label: 'Web Search', sub: 'SearXNG', x: 522, y: 425, w: 105, h: 46, color: '#34d399', glow: '#34d399', icon: '🔍', group: 'tools' },
  { id: 'focus', label: 'Focus', sub: 'Pomodoro', x: 168, y: 480, w: 105, h: 46, color: '#fbbf24', glow: '#fbbf24', icon: '🎯', group: 'tools' },
  { id: 'finance', label: 'Finance', sub: 'YNAB', x: 286, y: 480, w: 105, h: 46, color: '#34d399', glow: '#34d399', icon: '💰', group: 'tools' },

  // Output layer
  { id: 'tts', label: 'Jessica TTS', sub: 'Qwen3-TTS · Voice Clone', x: 180, y: 565, w: 170, h: 52, color: '#fb7185', glow: '#fb7185', icon: '🗣️', group: 'output' },
  { id: 'speakers', label: 'All Speakers', sub: 'Google Home Group', x: 420, y: 565, w: 160, h: 52, color: '#fbbf24', glow: '#fbbf24', icon: '🔊', group: 'output' },

  // Background jobs (right side)
  { id: 'bg_calendar', label: 'Calendar Poll', sub: 'Every 15 min', x: 700, y: 115, w: 140, h: 46, color: '#fb7185', glow: '#fb7185', icon: '⏰', group: 'background' },
  { id: 'bg_travel', label: 'Travel Time', sub: 'Google Maps API', x: 700, y: 172, w: 140, h: 46, color: '#fbbf24', glow: '#fbbf24', icon: '🚗', group: 'background' },
  { id: 'bg_email', label: 'Email Poll', sub: 'Every 30 min', x: 700, y: 229, w: 140, h: 46, color: '#fb7185', glow: '#fb7185', icon: '📬', group: 'background' },
  { id: 'bg_morning', label: 'Morning Brief', sub: '7:00 AM Daily', x: 700, y: 286, w: 140, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '☀️', group: 'background' },
];

// ── Flow paths (animated sequences) ──────────────────────────────────────────

const FLOWS: FlowPath[] = [
  {
    name: '🎤 Voice Command',
    description: '"Hey Jess, turn on the bedroom lights to blue"',
    color: '#fbbf24',
    edges: [
      { from: 'user', to: 'echo', label: '"Hey Jess..."' },
      { from: 'echo', to: 'stt', label: 'Audio stream' },
      { from: 'stt', to: 'orchestrator', label: 'Transcribed text' },
      { from: 'orchestrator', to: 'brain', label: 'Tool call' },
      { from: 'brain', to: 'ha', label: 'Turn on lights' },
      { from: 'ha', to: 'brain', label: 'Success' },
      { from: 'brain', to: 'tts', label: '"Lights are blue"' },
      { from: 'tts', to: 'speakers', label: 'Audio' },
    ],
  },
  {
    name: '🚗 Travel Alert',
    description: '"Leave in 15 minutes — 20 min drive to Downtown"',
    color: '#fbbf24',
    edges: [
      { from: 'bg_calendar', to: 'calendar', label: 'Check events' },
      { from: 'calendar', to: 'bg_travel', label: 'Event with location' },
      { from: 'bg_travel', to: 'tts', label: '"Leave in 15 min"' },
      { from: 'tts', to: 'speakers', label: 'Announce' },
    ],
  },
  {
    name: '📧 Email Alert',
    description: 'New email from your boss — announced on speakers',
    color: '#fb7185',
    edges: [
      { from: 'bg_email', to: 'email', label: 'Check inbox' },
      { from: 'email', to: 'tts', label: '"New email from..."' },
      { from: 'tts', to: 'speakers', label: 'Announce' },
    ],
  },
  {
    name: '☀️ Morning Briefing',
    description: '7:00 AM — Today\'s events, reminders, and weather',
    color: '#a78bfa',
    edges: [
      { from: 'bg_morning', to: 'calendar', label: "Today's events" },
      { from: 'calendar', to: 'tts', label: 'Briefing text' },
      { from: 'tts', to: 'speakers', label: 'Bedroom speakers' },
    ],
  },
  {
    name: '💬 Chat Question',
    description: '"Jess, what pattern do I fall into when I feel rejected?"',
    color: '#34d399',
    edges: [
      { from: 'user', to: 'webui', label: 'Type or speak' },
      { from: 'webui', to: 'orchestrator', label: 'Message' },
      { from: 'orchestrator', to: 'router', label: 'Classify' },
      { from: 'router', to: 'brain', label: 'Mirror mode' },
      { from: 'brain', to: 'memory', label: 'RAG query' },
      { from: 'memory', to: 'brain', label: 'Pattern docs' },
      { from: 'brain', to: 'webui', label: 'Personalized response' },
    ],
  },
];

// ── Helpers ──────────────────────────────────────────────────────────────────

function getNode(id: NodeId): DiagramNode {
  return NODES.find((n) => n.id === id)!;
}

function nodeCenter(n: DiagramNode): [number, number] {
  return [n.x + n.w / 2, n.y + n.h / 2];
}

function edgePath(from: DiagramNode, to: DiagramNode): string {
  const [x1, y1] = nodeCenter(from);
  const [x2, y2] = nodeCenter(to);
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  // Slight curve
  const dx = x2 - x1;
  const dy = y2 - y1;
  const cx = mx - dy * 0.12;
  const cy = my + dx * 0.12;
  return `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
}

// ── Component ────────────────────────────────────────────────────────────────

export default function SystemDiagram() {
  const [activeFlow, setActiveFlow] = useState(0);
  const [activeStep, setActiveStep] = useState(-1);
  const [isPlaying, setIsPlaying] = useState(true);
  const [hoveredNode, setHoveredNode] = useState<NodeId | null>(null);

  const flow = FLOWS[activeFlow];

  // Auto-advance animation
  const advanceStep = useCallback(() => {
    setActiveStep((prev) => {
      if (prev >= flow.edges.length) {
        // Move to next flow
        setActiveFlow((f) => (f + 1) % FLOWS.length);
        return -1;
      }
      return prev + 1;
    });
  }, [flow.edges.length]);

  useEffect(() => {
    if (!isPlaying) return;
    const timer = setInterval(advanceStep, 1200);
    return () => clearInterval(timer);
  }, [isPlaying, advanceStep]);

  // Reset step when flow changes
  useEffect(() => {
    setActiveStep(-1);
  }, [activeFlow]);

  // Which nodes are "lit up" in the current animation
  const activeNodes = new Set<NodeId>();
  const activeEdges = new Set<number>();
  if (activeStep >= 0) {
    for (let i = 0; i <= Math.min(activeStep, flow.edges.length - 1); i++) {
      activeNodes.add(flow.edges[i].from);
      activeNodes.add(flow.edges[i].to);
      activeEdges.add(i);
    }
  }

  const svgWidth = 880;
  const svgHeight = 640;

  return (
    <Card padding="none" className="p-4 md:p-6">
      <h2 className="text-lg md:text-xl font-bold text-white mb-1">
        How Jess Works
      </h2>
      <p className="text-xs md:text-sm text-content-secondary mb-4">
        Tap a scenario to see data flow through the system in real time
      </p>

      {/* Flow selector pills */}
      <div className="flex flex-wrap gap-2 mb-4">
        {FLOWS.map((f, i) => (
          <button
            key={f.name}
            onClick={() => { setActiveFlow(i); setActiveStep(-1); setIsPlaying(true); }}
            className={`px-3 py-1.5 rounded-full text-xs font-medium transition-all duration-300 ${
              i === activeFlow
                ? 'text-white shadow-lg scale-105'
                : 'bg-surface-raised/60 text-content-secondary hover:bg-surface-overlay/60 hover:text-content-primary'
            }`}
            style={i === activeFlow ? { background: f.color + '30', boxShadow: `0 0 20px ${f.color}30`, border: `1px solid ${f.color}60` } : {}}
          >
            {f.name}
          </button>
        ))}
        <button
          onClick={() => setIsPlaying(!isPlaying)}
          className="ml-auto px-3 py-1.5 rounded-full text-xs font-medium bg-surface-raised/60 text-content-secondary hover:text-white transition-colors"
        >
          {isPlaying ? '⏸ Pause' : '▶ Play'}
        </button>
      </div>

      {/* Active flow description */}
      <div
        className="mb-4 px-4 py-2.5 rounded-lg text-sm transition-all duration-500"
        style={{ background: flow.color + '15', borderLeft: `3px solid ${flow.color}` }}
      >
        <span className="text-content-primary">{flow.description}</span>
      </div>

      {/* SVG Diagram */}
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${svgWidth} ${svgHeight}`}
          className="w-full min-w-[800px]"
          style={{ maxHeight: '560px' }}
        >
          <defs>
            {/* Glow filters for each node color */}
            {NODES.map((n) => (
              <filter key={`glow-${n.id}`} id={`glow-${n.id}`} x="-50%" y="-50%" width="200%" height="200%">
                <feGaussianBlur stdDeviation="6" result="blur" />
                <feFlood floodColor={n.glow} floodOpacity="0.6" />
                <feComposite in2="blur" operator="in" />
                <feMerge>
                  <feMergeNode />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            ))}
            {/* Animated dash for active edges */}
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="6" markerHeight="6" orient="auto-start-reverse"
              fill={flow.color}
            >
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
            {/* Pulse animation */}
            <radialGradient id="pulse-grad">
              <stop offset="0%" stopColor={flow.color} stopOpacity="0.8" />
              <stop offset="100%" stopColor={flow.color} stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* Group labels */}
          <text x="20" y="20" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Input</text>
          <text x="20" y="199" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Processing</text>
          <text x="20" y="314" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">AI Model</text>
          <text x="20" y="420" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Tools</text>
          <text x="20" y="560" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Output</text>
          <text x="688" y="104" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Background</text>

          {/* Background region for background jobs */}
          <rect x="688" y="108" width="164" height="236" rx="12"
            fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.09)" strokeDasharray="4 4" />

          {/* Edges (draw all potential edges dimly, active ones brightly) */}
          {flow.edges.map((edge, i) => {
            const fromNode = getNode(edge.from);
            const toNode = getNode(edge.to);
            const path = edgePath(fromNode, toNode);
            const isActive = activeEdges.has(i);
            const isCurrentStep = i === activeStep;

            return (
              <g key={`edge-${i}`}>
                {/* Base path (dim) */}
                <path
                  d={path}
                  fill="none"
                  stroke={isActive ? flow.color : 'rgba(255,255,255,0.09)'}
                  strokeWidth={isActive ? 2.5 : 1}
                  strokeOpacity={isActive ? 0.8 : 0.3}
                  markerEnd={isActive ? 'url(#arrow)' : undefined}
                  className="transition-all duration-500"
                />
                {/* Animated dash overlay for current step */}
                {isCurrentStep && (
                  <path
                    d={path}
                    fill="none"
                    stroke={flow.color}
                    strokeWidth={3}
                    strokeDasharray="8 6"
                    markerEnd="url(#arrow)"
                    opacity={0.9}
                  >
                    <animate attributeName="stroke-dashoffset" from="28" to="0" dur="0.8s" repeatCount="indefinite" />
                  </path>
                )}
                {/* Edge label */}
                {isActive && edge.label && (
                  <g>
                    {(() => {
                      const [x1, y1] = nodeCenter(fromNode);
                      const [x2, y2] = nodeCenter(toNode);
                      const lx = (x1 + x2) / 2;
                      const ly = (y1 + y2) / 2 - 8;
                      return (
                        <>
                          <rect x={lx - edge.label.length * 3.2} y={ly - 9} width={edge.label.length * 6.4} height={16}
                            rx="4" fill="#14151fd9" stroke={flow.color + '40'} strokeWidth={0.5} />
                          <text x={lx} y={ly + 3} textAnchor="middle"
                            className="text-[9px]" fill={flow.color} opacity={isCurrentStep ? 1 : 0.7}>
                            {edge.label}
                          </text>
                        </>
                      );
                    })()}
                  </g>
                )}
              </g>
            );
          })}

          {/* Nodes */}
          {NODES.map((node) => {
            const isActive2 = activeNodes.has(node.id);
            const isHovered = hoveredNode === node.id;
            const lit = isActive2 || isHovered;

            return (
              <g
                key={node.id}
                onMouseEnter={() => setHoveredNode(node.id)}
                onMouseLeave={() => setHoveredNode(null)}
                className="cursor-pointer"
              >
                {/* Glow pulse when active */}
                {lit && (
                  <rect
                    x={node.x - 4} y={node.y - 4}
                    width={node.w + 8} height={node.h + 8}
                    rx="14"
                    fill="none"
                    stroke={node.glow}
                    strokeWidth={2}
                    opacity={0.5}
                  >
                    <animate attributeName="opacity" values="0.5;0.2;0.5" dur="2s" repeatCount="indefinite" />
                  </rect>
                )}

                {/* Node body */}
                <rect
                  x={node.x} y={node.y}
                  width={node.w} height={node.h}
                  rx="10"
                  fill={lit ? node.color + '25' : '#14151fe6'}
                  stroke={lit ? node.color : 'rgba(255,255,255,0.09)'}
                  strokeWidth={lit ? 1.5 : 0.5}
                  className="transition-all duration-300"
                />

                {/* Icon */}
                <text
                  x={node.x + 12} y={node.y + node.h / 2 + 1}
                  dominantBaseline="middle"
                  className="text-[14px]"
                >
                  {node.icon}
                </text>

                {/* Label */}
                <text
                  x={node.x + 30} y={node.y + node.h / 2 - 5}
                  dominantBaseline="middle"
                  className="text-[11px] font-bold"
                  fill={lit ? '#fff' : '#aaa9b8'}
                >
                  {node.label}
                </text>

                {/* Sublabel */}
                <text
                  x={node.x + 30} y={node.y + node.h / 2 + 9}
                  dominantBaseline="middle"
                  className="text-[8px]"
                  fill={lit ? node.color : '#84838f'}
                >
                  {node.sub}
                </text>
              </g>
            );
          })}

          {/* Animated pulse dot traveling along current edge */}
          {activeStep >= 0 && activeStep < flow.edges.length && (() => {
            const edge = flow.edges[activeStep];
            const fromNode = getNode(edge.from);
            const toNode = getNode(edge.to);
            const path = edgePath(fromNode, toNode);
            return (
              <circle r="5" fill={flow.color} opacity={0.9}>
                <animateMotion dur="0.8s" repeatCount="indefinite" path={path} />
              </circle>
            );
          })()}
        </svg>
      </div>

      {/* Step progress bar */}
      <div className="mt-3 flex items-center gap-1.5">
        {flow.edges.map((_, i) => (
          <div
            key={i}
            className="h-1 rounded-full transition-all duration-300 flex-1"
            style={{
              background: i <= activeStep ? flow.color : 'rgba(255,255,255,0.09)',
              opacity: i <= activeStep ? 1 : 0.5,
            }}
          />
        ))}
      </div>
    </Card>
  );
}
