'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card } from '@/components/ui';
import { useHealth, useServices } from '@/lib/hooks';
import type { ServicesResponse } from '@/lib/types';

// ── Types ────────────────────────────────────────────────────────────────────

type NodeId =
  | 'user' | 'echo' | 'stt' | 'frontend'
  | 'orchestrator' | 'router' | 'plug' | 'brain' | 'vision'
  | 'ha' | 'memory' | 'calendar' | 'email' | 'web' | 'focus' | 'finance'
  | 'tts' | 'speakers' | 'push'
  | 'monitoring' | 'backups'
  | 'bg_calendar' | 'bg_travel' | 'bg_reminders' | 'bg_selfcare'
  | 'bg_morning' | 'bg_evening' | 'bg_winddown';

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
  /**
   * Key in the orchestrator `/api/services` summary that backs this node, if
   * any. When set, the node shows a LIVE health tint (green/amber/red)
   * independent of the decorative flow animation.
   */
  service?: string;
  /** Wired up but switched off on this deployment — drawn dimmed + dashed. */
  disabled?: boolean;
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
//
// Ground truth: CLAUDE.md "Cluster" + "Services" tables and the scheduler
// registrations in orchestrator/orchestrator.py. Times shown are the defaults
// (MORNING_BRIEFING_TIME, EVENING_BRIEFING_TIME, WIND_DOWN_BEDTIME).

const NODES: DiagramNode[] = [
  // Input layer
  { id: 'user', label: 'You', sub: 'Voice or Text', x: 330, y: 30, w: 120, h: 56, color: '#fbbf24', glow: '#fbbf24', icon: '🎤', group: 'input' },
  // HA Assist voice path (ATOM Echo → Wyoming) is off: HA's conversation agent
  // still points at the old orchestrator host. Shown so it isn't forgotten.
  { id: 'echo', label: 'ATOM Echo', sub: 'Voice · disabled', x: 140, y: 115, w: 130, h: 52, color: '#84838f', glow: '#84838f', icon: '📡', group: 'input', disabled: true },
  { id: 'frontend', label: 'Dashboard', sub: 'Next.js · HTTPS · Jupiter', x: 510, y: 115, w: 150, h: 52, color: '#84838f', glow: '#84838f', icon: '🌐', group: 'input' },

  // Processing layer
  { id: 'stt', label: 'Parakeet STT', sub: 'TDT v2 · ONNX CPU · Helios', x: 130, y: 205, w: 150, h: 52, color: '#a78bfa', glow: '#a78bfa', icon: '👂', group: 'processing', service: 'stt' },
  { id: 'orchestrator', label: 'Orchestrator', sub: 'Unified loop · Jupiter', x: 320, y: 205, w: 160, h: 60, color: '#6f63ff', glow: '#6f63ff', icon: '🧠', group: 'processing' },
  { id: 'router', label: 'Mode Router', sub: 'Intent + Emotion', x: 520, y: 205, w: 140, h: 52, color: '#6f63ff', glow: '#6f63ff', icon: '🎯', group: 'processing' },

  // AI layer — Helios is power-tiered: asleep most of the time, woken by an
  // HA-controlled smart plug when a chat needs the brain.
  { id: 'plug', label: 'Smart Plug', sub: 'via HA · wakes Helios', x: 110, y: 320, w: 150, h: 52, color: '#fbbf24', glow: '#fbbf24', icon: '🔌', group: 'ai' },
  { id: 'brain', label: 'Qwen3.8 TURBO', sub: 'llama.cpp · RTX 5090 · Helios', x: 300, y: 318, w: 200, h: 56, color: '#34d399', glow: '#34d399', icon: '🧠', group: 'ai', service: 'model' },
  { id: 'vision', label: 'Vision', sub: 'Served by the brain', x: 530, y: 320, w: 130, h: 52, color: '#34d399', glow: '#34d399', icon: '👁️', group: 'ai', service: 'vision' },

  // Tools layer (two rows)
  { id: 'ha', label: 'Home Assistant', sub: 'Jupiter · :8123', x: 30, y: 425, w: 132, h: 46, color: '#34d3e0', glow: '#34d3e0', icon: '🏠', group: 'tools', service: 'ha' },
  { id: 'memory', label: 'Memory', sub: 'MemPalace', x: 170, y: 425, w: 116, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '🧩', group: 'tools' },
  { id: 'calendar', label: 'Calendar', sub: 'Radicale CalDAV', x: 294, y: 425, w: 116, h: 46, color: '#fb7185', glow: '#fb7185', icon: '📅', group: 'tools' },
  { id: 'email', label: 'Email', sub: 'Gmail · read-only', x: 418, y: 425, w: 116, h: 46, color: '#fb7185', glow: '#fb7185', icon: '📧', group: 'tools' },
  { id: 'web', label: 'Web Search', sub: 'SearXNG', x: 542, y: 425, w: 120, h: 46, color: '#34d399', glow: '#34d399', icon: '🔍', group: 'tools', service: 'searxng' },
  { id: 'focus', label: 'Focus', sub: 'Pomodoro', x: 170, y: 480, w: 116, h: 46, color: '#fbbf24', glow: '#fbbf24', icon: '🎯', group: 'tools' },
  { id: 'finance', label: 'Actual Budget', sub: 'Finance · read-only', x: 294, y: 480, w: 116, h: 46, color: '#34d399', glow: '#34d399', icon: '💰', group: 'tools' },

  // Output layer
  { id: 'tts', label: 'Jessica TTS', sub: 'Voice clone · Helios', x: 30, y: 575, w: 170, h: 52, color: '#fb7185', glow: '#fb7185', icon: '🗣️', group: 'output', service: 'tts' },
  { id: 'speakers', label: 'Speakers', sub: 'Google Home / Cast', x: 230, y: 575, w: 160, h: 52, color: '#fbbf24', glow: '#fbbf24', icon: '🔊', group: 'output' },
  { id: 'push', label: 'Phone Push', sub: 'Pushover · ntfy', x: 420, y: 575, w: 160, h: 52, color: '#fbbf24', glow: '#fbbf24', icon: '📱', group: 'output' },

  // Infrastructure (Jupiter)
  { id: 'monitoring', label: 'Monitoring', sub: 'Prometheus · Grafana · Loki', x: 30, y: 670, w: 200, h: 48, color: '#84838f', glow: '#84838f', icon: '📈', group: 'infra' },
  { id: 'backups', label: 'Nightly Backups', sub: 'Jupiter → Saturn', x: 260, y: 670, w: 190, h: 48, color: '#84838f', glow: '#84838f', icon: '💾', group: 'infra' },

  // Background jobs (right side)
  { id: 'bg_calendar', label: 'Calendar Poll', sub: 'Every 5 min', x: 700, y: 115, w: 150, h: 46, color: '#fb7185', glow: '#fb7185', icon: '⏰', group: 'background' },
  { id: 'bg_travel', label: 'Travel Time', sub: 'Google Maps API', x: 700, y: 169, w: 150, h: 46, color: '#fbbf24', glow: '#fbbf24', icon: '🚗', group: 'background' },
  { id: 'bg_reminders', label: 'Reminders', sub: 'Voice + phone push', x: 700, y: 223, w: 150, h: 46, color: '#fb7185', glow: '#fb7185', icon: '🔔', group: 'background' },
  { id: 'bg_selfcare', label: 'Self-care Nudges', sub: 'Every 15 min', x: 700, y: 277, w: 150, h: 46, color: '#34d3e0', glow: '#34d3e0', icon: '💧', group: 'background' },
  { id: 'bg_morning', label: 'Morning Brief', sub: '07:00 daily', x: 700, y: 331, w: 150, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '☀️', group: 'background' },
  { id: 'bg_evening', label: 'Evening Brief', sub: '21:30 daily', x: 700, y: 385, w: 150, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '🌙', group: 'background' },
  { id: 'bg_winddown', label: 'Wind-down', sub: 'Before 22:30 bedtime', x: 700, y: 439, w: 150, h: 46, color: '#a78bfa', glow: '#a78bfa', icon: '😴', group: 'background' },
];

// ── Flow paths (animated sequences) ──────────────────────────────────────────

const FLOWS: FlowPath[] = [
  {
    name: '🎙️ Voice Command',
    description: 'Tap the dashboard mic: "turn on the bedroom lights"',
    color: '#fbbf24',
    edges: [
      { from: 'user', to: 'frontend', label: 'Mic' },
      { from: 'frontend', to: 'orchestrator', label: 'Audio' },
      { from: 'orchestrator', to: 'stt', label: 'Transcribe' },
      { from: 'stt', to: 'orchestrator', label: 'Text' },
      { from: 'orchestrator', to: 'brain', label: 'Message + tools' },
      { from: 'brain', to: 'ha', label: 'Turn on lights' },
      { from: 'ha', to: 'brain', label: 'Done' },
      { from: 'brain', to: 'tts', label: '"Lights are on"' },
      { from: 'tts', to: 'frontend', label: 'Spoken reply' },
    ],
  },
  {
    name: '😴 Waking the Brain',
    description: 'Helios sleeps most of the time — with wake-on-demand on, a chat powers it on',
    color: '#34d399',
    edges: [
      { from: 'user', to: 'frontend', label: 'Message' },
      { from: 'frontend', to: 'orchestrator', label: 'Chat' },
      { from: 'orchestrator', to: 'ha', label: 'Wake Helios' },
      { from: 'ha', to: 'plug', label: 'Plug on' },
      { from: 'plug', to: 'brain', label: 'Helios boots' },
      { from: 'orchestrator', to: 'frontend', label: '"Waking up…"' },
    ],
  },
  {
    name: '💬 Chat Question',
    description: '"Jess, what pattern do I fall into when I feel rejected?"',
    color: '#6f63ff',
    edges: [
      { from: 'user', to: 'frontend', label: 'Type or speak' },
      { from: 'frontend', to: 'orchestrator', label: 'Message' },
      { from: 'orchestrator', to: 'router', label: 'Classify' },
      { from: 'router', to: 'brain', label: 'Mirror mode' },
      { from: 'brain', to: 'memory', label: 'Search memory' },
      { from: 'memory', to: 'brain', label: 'Memories' },
      { from: 'brain', to: 'frontend', label: 'Personalized response' },
    ],
  },
  {
    name: '📷 Photo',
    description: 'Share a photo — the brain itself does the vision',
    color: '#34d399',
    edges: [
      { from: 'user', to: 'frontend', label: 'Photo' },
      { from: 'frontend', to: 'orchestrator', label: 'Upload' },
      { from: 'orchestrator', to: 'vision', label: 'Analyze image' },
      { from: 'vision', to: 'frontend', label: 'Description' },
    ],
  },
  {
    name: '🔔 Reminders & Nudges',
    description: 'Spoken on the speakers, with Done / Snooze on your phone',
    color: '#fb7185',
    edges: [
      { from: 'bg_reminders', to: 'tts', label: '"Time to…"' },
      { from: 'tts', to: 'speakers', label: 'Announce' },
      { from: 'bg_reminders', to: 'push', label: 'Done / Snooze' },
      { from: 'push', to: 'orchestrator', label: 'Tap Done' },
      { from: 'bg_selfcare', to: 'tts', label: '"Had water?"' },
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
    name: '☀️ Morning Briefing',
    description: "07:00 — today's events and weather, spoken in the bedroom",
    color: '#a78bfa',
    edges: [
      { from: 'bg_morning', to: 'calendar', label: "Today's events" },
      { from: 'calendar', to: 'tts', label: 'Briefing text' },
      { from: 'tts', to: 'speakers', label: 'Bedroom speakers' },
    ],
  },
  {
    name: '🌙 Evening & Wind-down',
    description: '21:30 shutdown ritual, then lights dim and a screens-away nudge',
    color: '#a78bfa',
    edges: [
      { from: 'bg_evening', to: 'calendar', label: "Tomorrow's first event" },
      { from: 'calendar', to: 'tts', label: 'Leave-by + meds check' },
      { from: 'tts', to: 'speakers', label: 'Announce' },
      { from: 'bg_winddown', to: 'ha', label: 'Dim scene' },
      { from: 'bg_winddown', to: 'tts', label: '"Screens away"' },
    ],
  },
  {
    name: '🛡️ Ops & Backups',
    description: 'Metrics, logs and alerts on Jupiter; state copied to Saturn nightly',
    color: '#84838f',
    edges: [
      { from: 'orchestrator', to: 'monitoring', label: 'Metrics + logs' },
      { from: 'monitoring', to: 'push', label: 'Alert (Pushover)' },
      { from: 'orchestrator', to: 'backups', label: 'DBs + memory' },
      { from: 'finance', to: 'backups', label: 'Budget' },
      { from: 'calendar', to: 'backups', label: 'Calendars' },
      { from: 'ha', to: 'backups', label: 'HA config' },
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

// Quadratic-curve control point for an edge (slight curve; the offset flips
// with direction, so A→B and B→A don't share a path or a label spot).
function edgeCurve(from: DiagramNode, to: DiagramNode) {
  const [x1, y1] = nodeCenter(from);
  const [x2, y2] = nodeCenter(to);
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const dx = x2 - x1;
  const dy = y2 - y1;
  return { x1, y1, x2, y2, mx, my, cx: mx - dy * 0.12, cy: my + dx * 0.12 };
}

function edgePath(from: DiagramNode, to: DiagramNode): string {
  const { x1, y1, x2, y2, cx, cy } = edgeCurve(from, to);
  return `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
}

// Point on the drawn curve at t=0.5: B(0.5) = (M + C) / 2.
function edgeLabelPoint(from: DiagramNode, to: DiagramNode): [number, number] {
  const { mx, my, cx, cy } = edgeCurve(from, to);
  return [(mx + cx) / 2, (my + cy) / 2];
}

// LIVE health dot color for a service-backed node, from /api/services.
// Returns null when the node isn't service-backed or health is unknown, so we
// don't render a misleading dot. Helios services (model/vision/tts/stt) report as
// amber ("configured but not healthy" = asleep) rather than red.
const HELIOS_SERVICES = new Set(['model', 'vision', 'tts', 'stt']);

function serviceDotColor(
  node: DiagramNode,
  services: ServicesResponse | undefined,
): string | null {
  if (!node.service || !services) return null;
  const info = services.services[node.service];
  if (!info) return null; // unconfigured — nothing to show
  if (info.healthy) return '#34d399'; // green
  return HELIOS_SERVICES.has(node.service) ? '#fbbf24' : '#fb7185'; // amber : red
}

// ── Component ────────────────────────────────────────────────────────────────

export default function SystemDiagram() {
  const [activeFlow, setActiveFlow] = useState(0);
  const [activeStep, setActiveStep] = useState(-1);
  const [isPlaying, setIsPlaying] = useState(true);
  const [hoveredNode, setHoveredNode] = useState<NodeId | null>(null);

  // Live data — memory count and per-service health.
  const { data: health } = useHealth();
  const { data: services } = useServices();
  const ragDocs = health?.rag_docs;

  // Live sublabel overrides keyed by node id (empty until data loads).
  const liveSub: Partial<Record<NodeId, string>> = {
    ...(ragDocs != null ? { memory: `MemPalace · ${ragDocs}` } : {}),
  };

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
  const svgHeight = 740;

  return (
    <Card padding="none" className="p-4 md:p-6">
      <h2 className="text-lg md:text-xl font-bold text-content-primary mb-1">
        How Jess Works
      </h2>
      <p className="text-xs md:text-sm text-content-secondary mb-3">
        Tap a scenario to see data flow through the system in real time
      </p>

      {/* Live-status legend — the dots on service-backed nodes are real health */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-1 text-[11px] text-content-muted">
        <span className="inline-flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full" style={{ background: '#34d399' }} /> Live
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full" style={{ background: '#fbbf24' }} /> Asleep (Helios)
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full" style={{ background: '#fb7185' }} /> Down
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 h-2 rounded-sm border border-dashed border-content-muted" /> Disabled
        </span>
      </div>
      <p className="text-[11px] text-content-muted mb-4">
        Not shown: the code agent (retired) and the expert model (deprecated).
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
          className="ml-auto px-3 py-1.5 rounded-full text-xs font-medium bg-surface-raised/60 text-content-secondary hover:text-content-primary transition-colors"
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
          style={{ maxHeight: '740px' }}
          role="img"
          aria-label={`System diagram: ${flow.name} — ${flow.description}`}
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
          <text x="20" y="310" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">AI Model · Helios (asleep until needed)</text>
          <text x="20" y="420" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Tools</text>
          <text x="20" y="568" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Output</text>
          <text x="20" y="663" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Infrastructure · Jupiter</text>
          <text x="688" y="104" className="fill-content-muted text-[10px] font-bold uppercase tracking-widest">Background</text>

          {/* Background region for background jobs */}
          <rect x="688" y="108" width="174" height="386" rx="12"
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
              </g>
            );
          })}

          {/* Nodes */}
          {NODES.map((node) => {
            const isActive2 = activeNodes.has(node.id);
            const isHovered = hoveredNode === node.id;
            const lit = isActive2 || isHovered;
            const dotColor = serviceDotColor(node, services);
            const sub = liveSub[node.id] ?? node.sub;

            return (
              <g
                key={node.id}
                onMouseEnter={() => setHoveredNode(node.id)}
                onMouseLeave={() => setHoveredNode(null)}
                className="cursor-pointer"
                opacity={node.disabled && !lit ? 0.5 : 1}
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
                  stroke={lit ? node.color : node.disabled ? 'rgba(255,255,255,0.35)' : 'rgba(255,255,255,0.09)'}
                  strokeWidth={lit ? 1.5 : 0.5}
                  strokeDasharray={node.disabled ? '4 3' : undefined}
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
                  {sub}
                </text>

                {/* LIVE health dot (service-backed nodes only) — reflects real
                    /api/services health, independent of the flow animation. */}
                {dotColor && (
                  <circle
                    cx={node.x + node.w - 9}
                    cy={node.y + 9}
                    r={3.5}
                    fill={dotColor}
                  >
                    {dotColor === '#34d399' && (
                      <animate attributeName="opacity" values="1;0.4;1" dur="2.5s" repeatCount="indefinite" />
                    )}
                  </circle>
                )}
              </g>
            );
          })}

          {/* Edge labels — drawn after the nodes so a label over a node stays readable */}
          {flow.edges.map((edge, i) => {
            if (!activeEdges.has(i) || !edge.label) return null;
            const [lx, mid] = edgeLabelPoint(getNode(edge.from), getNode(edge.to));
            const ly = mid - 8;
            return (
              <g key={`label-${i}`}>
                <rect x={lx - edge.label.length * 3.2} y={ly - 9} width={edge.label.length * 6.4} height={16}
                  rx="4" fill="#14151fd9" stroke={flow.color + '40'} strokeWidth={0.5} />
                <text x={lx} y={ly + 3} textAnchor="middle"
                  className="text-[9px]" fill={flow.color} opacity={i === activeStep ? 1 : 0.7}>
                  {edge.label}
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
