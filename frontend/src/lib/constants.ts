export const ORCHESTRATOR_URL =
  process.env.ORCHESTRATOR_URL || 'http://localhost:8888';

export const CLUSTER_NODES = [
  {
    id: 'helios',
    name: 'Helios',
    ip: '10.0.0.195',
    gpu: 'RTX 5090 + RTX PRO 5000',
    role: 'Gateway + Primary LLM + TTS/STT + Code agent',
    services: [
      'Orchestrator',
      'Open WebUI',
      'Qwen3.5-27B (primary)',
      'Qwen2.5-Coder-32B (code agent)',
      'Qwen3-TTS',
      'Whisper STT',
      'Frontend',
    ],
  },
  {
    id: 'saturn',
    name: 'Saturn',
    ip: '10.0.0.58',
    gpu: 'RTX 3080 + RTX 3090',
    role: 'Vision model',
    services: ['Qwen2.5-VL-7B', 'Pi-hole secondary'],
  },
  {
    id: 'uranus',
    name: 'Uranus',
    ip: '10.0.0.173',
    gpu: '2x RTX 5080',
    role: 'ComfyUI / Conjure',
    services: ['ComfyUI'],
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
