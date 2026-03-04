'use client';

import { useEffect, useState } from 'react';
import { Shield, Sparkles } from 'lucide-react';
import { LEVELS } from '@/lib/finance-constants';

interface Props {
  level: number;
  onDismiss: () => void;
}

const TIER_COLORS: Record<number, { bg: string; text: string; glow: string }> = {
  1: { bg: 'from-orange-900/80 to-orange-700/80', text: 'text-orange-300', glow: 'shadow-orange-500/30' },
  2: { bg: 'from-amber-900/80 to-amber-700/80', text: 'text-amber-300', glow: 'shadow-amber-500/30' },
  3: { bg: 'from-slate-700/80 to-slate-500/80', text: 'text-slate-200', glow: 'shadow-slate-400/30' },
  4: { bg: 'from-yellow-800/80 to-yellow-600/80', text: 'text-yellow-300', glow: 'shadow-yellow-400/30' },
  5: { bg: 'from-zinc-600/80 to-zinc-400/80', text: 'text-zinc-100', glow: 'shadow-zinc-300/30' },
  6: { bg: 'from-cyan-800/80 to-cyan-600/80', text: 'text-cyan-200', glow: 'shadow-cyan-400/30' },
  7: { bg: 'from-emerald-800/80 to-emerald-600/80', text: 'text-emerald-200', glow: 'shadow-emerald-400/30' },
  8: { bg: 'from-blue-800/80 to-blue-600/80', text: 'text-blue-200', glow: 'shadow-blue-400/30' },
  9: { bg: 'from-red-800/80 to-red-600/80', text: 'text-red-200', glow: 'shadow-red-400/30' },
  10: { bg: 'from-zinc-900/80 to-zinc-700/80', text: 'text-zinc-100', glow: 'shadow-zinc-500/30' },
  11: { bg: 'from-amber-700/80 to-yellow-500/80', text: 'text-yellow-100', glow: 'shadow-yellow-400/50' },
};

export default function LevelUpOverlay({ level, onDismiss }: Props) {
  const [visible, setVisible] = useState(false);

  const levelInfo = LEVELS.find((l) => l.level === level);
  const title = levelInfo?.title || `Level ${level}`;
  const colors = TIER_COLORS[level] || TIER_COLORS[1];

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));

    const timer = setTimeout(() => {
      setVisible(false);
      setTimeout(onDismiss, 500);
    }, 5000);

    return () => clearTimeout(timer);
  }, [onDismiss]);

  return (
    <div
      className={`fixed inset-0 z-[100] flex items-center justify-center transition-all duration-500 ${
        visible ? 'opacity-100' : 'opacity-0'
      }`}
      onClick={() => {
        setVisible(false);
        setTimeout(onDismiss, 500);
      }}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />

      {/* Content */}
      <div
        className={`relative transition-all duration-700 ${
          visible ? 'scale-100 translate-y-0' : 'scale-75 translate-y-8'
        }`}
      >
        {/* Sparkle decorations */}
        <Sparkles
          size={24}
          className={`absolute -top-6 -left-4 ${colors.text} animate-pulse`}
        />
        <Sparkles
          size={16}
          className={`absolute -top-3 right-2 ${colors.text} animate-bounce`}
        />
        <Sparkles
          size={20}
          className={`absolute bottom-0 -right-6 ${colors.text} animate-pulse`}
        />

        {/* Card */}
        <div
          className={`bg-gradient-to-br ${colors.bg} rounded-2xl p-10 text-center shadow-2xl ${colors.glow} border border-white/10`}
        >
          <div className={`${colors.text} mb-3`}>
            <Shield size={56} className="mx-auto" />
          </div>
          <p className="text-sm text-white/60 uppercase tracking-widest mb-1">
            Level Up!
          </p>
          <h1 className={`text-3xl font-black ${colors.text} mb-1`}>
            Level {level}
          </h1>
          <p className="text-lg font-semibold text-white/90">{title}</p>
          <p className="text-xs text-white/40 mt-4">Tap to dismiss</p>
        </div>
      </div>
    </div>
  );
}
