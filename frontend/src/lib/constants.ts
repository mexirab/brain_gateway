export const ORCHESTRATOR_URL =
  process.env.ORCHESTRATOR_URL || 'http://localhost:8888';

export const PUBLIC_ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8888';

export const CLUSTER_NODES = [
  {
    id: 'jupiter',
    name: 'Jupiter',
    ip: '10.0.0.248',
    gpu: null,
    role: 'Gateway & Docker host',
    services: ['Orchestrator', 'Open WebUI', 'Pi-hole', 'SearXNG', 'Frontend'],
  },
  {
    id: 'saturn',
    name: 'Saturn',
    ip: '10.0.0.58',
    gpu: 'RTX 3090',
    role: 'Qwen3.5-9B (fallback)',
    services: ['llama.cpp', 'Pi-hole secondary'],
  },
  {
    id: 'uranus',
    name: 'Uranus',
    ip: '10.0.0.173',
    gpu: '2x RTX 5080',
    role: 'TTS & STT',
    services: ['Qwen3-TTS (Jessica)', 'Whisper STT'],
  },
  {
    id: 'helios',
    name: 'Helios',
    ip: '10.0.0.195',
    gpu: 'RTX 5090',
    role: 'Qwen3.5-27B (brain)',
    services: ['llama.cpp'],
  },
  {
    id: 'ha',
    name: 'Home Assistant',
    ip: '10.0.0.106',
    gpu: null,
    role: 'Smart home hub',
    services: ['Home Assistant', 'Voice Pipeline'],
  },
] as const;

export const CAPABILITIES = [
  { icon: 'Mic', label: 'Voice Control', desc: '"Hey Jess" wake word' },
  { icon: 'Timer', label: 'Focus Timer', desc: 'Pomodoro + site blocking' },
  { icon: 'Calendar', label: 'Calendar', desc: 'Proactive scheduling' },
  { icon: 'Home', label: 'Smart Home', desc: 'Lights, climate, scenes' },
  { icon: 'Brain', label: 'Memory', desc: '154 personal knowledge docs' },
  { icon: 'Search', label: 'Web Search', desc: 'Real-time answers' },
] as const;
