'use client';

import { useEffect, useState } from 'react';
import { Shield, Sparkles } from 'lucide-react';
import { LEVELS } from '@/lib/finance-constants';
import { useReducedMotion } from '@/hooks/useReducedMotion';

interface Props {
  level: number;
  onDismiss: () => void;
}

const TIER_COLORS: Record<number, { bg: string; text: string; glow: string }> = {
  1: { bg: 'from-accent-flame/80 to-accent-flame/60', text: 'text-accent-flame', glow: 'shadow-accent-flame/30' },
  2: { bg: 'from-accent-flame/70 to-accent-gold/60', text: 'text-accent-flame', glow: 'shadow-accent-flame/30' },
  3: { bg: 'from-surface-overlay/80 to-surface-raised/80', text: 'text-content-secondary', glow: 'shadow-content-muted/30' },
  4: { bg: 'from-accent-gold/80 to-accent-gold/60', text: 'text-accent-gold', glow: 'shadow-accent-gold/30' },
  5: { bg: 'from-surface-overlay/80 to-content-muted/40', text: 'text-content-primary', glow: 'shadow-content-secondary/30' },
  6: { bg: 'from-accent-cyan/80 to-accent-cyan/60', text: 'text-accent-cyan', glow: 'shadow-accent-cyan/30' },
  7: { bg: 'from-success/80 to-success/60', text: 'text-success', glow: 'shadow-success/30' },
  8: { bg: 'from-info/80 to-info/60', text: 'text-info', glow: 'shadow-info/30' },
  9: { bg: 'from-danger/80 to-danger/60', text: 'text-danger', glow: 'shadow-danger/30' },
  10: { bg: 'from-surface-inset/80 to-surface-raised/80', text: 'text-content-primary', glow: 'shadow-content-muted/30' },
  11: { bg: 'from-accent-gold/80 to-accent-gold/60', text: 'text-accent-gold', glow: 'shadow-accent-gold/50' },
};

export default function LevelUpOverlay({ level, onDismiss }: Props) {
  const reduced = useReducedMotion();
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
          className={`absolute -top-6 -left-4 ${colors.text} ${reduced ? '' : 'animate-pulse'}`}
        />
        <Sparkles
          size={16}
          className={`absolute -top-3 right-2 ${colors.text} ${reduced ? '' : 'animate-bounce'}`}
        />
        <Sparkles
          size={20}
          className={`absolute bottom-0 -right-6 ${colors.text} ${reduced ? '' : 'animate-pulse'}`}
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
