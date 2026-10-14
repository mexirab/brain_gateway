'use client';

import { AudioLines, Loader2, PhoneOff, Square } from 'lucide-react';
import type { CallPhase } from '@/hooks/useVoiceCall';

interface VoiceCallBarProps {
  phase: CallPhase;
  /** Jess's reply audio is loading or playing. */
  speaking: boolean;
  onInterrupt: () => void;
  onEnd: () => void;
}

const FOCUS_RING = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50';

function label(phase: CallPhase, speaking: boolean): string {
  switch (phase) {
    case 'starting': return 'Starting…';
    case 'listening': return 'Listening — just talk';
    case 'hearing': return 'Hearing you…';
    case 'transcribing': return 'Got it…';
    case 'responding': return speaking ? 'Jess is speaking' : 'Jess is thinking…';
    default: return '';
  }
}

/** Replaces the text input while a hands-free call is active. */
export default function VoiceCallBar({ phase, speaking, onInterrupt, onEnd }: VoiceCallBarProps) {
  const live = phase === 'listening' || phase === 'hearing';
  const busy = phase === 'starting' || phase === 'transcribing' || (phase === 'responding' && !speaking);
  return (
    <div className="flex items-center gap-2">
      <div
        className={`flex-1 flex items-center gap-3 min-h-11 px-4 rounded-xl border text-sm ${
          phase === 'hearing'
            ? 'border-brand/60 bg-brand/15 text-content-primary'
            : 'border-line/50 bg-surface-raised/60 text-content-secondary'
        }`}
      >
        {busy ? (
          <Loader2 size={18} className="animate-spin shrink-0" aria-hidden />
        ) : (
          <AudioLines
            size={18}
            aria-hidden
            className={`shrink-0 ${live || speaking ? 'text-brand' : ''} ${phase === 'hearing' || speaking ? 'animate-pulse' : ''}`}
          />
        )}
        <span aria-live="polite">{label(phase, speaking)}</span>
      </div>
      {phase === 'responding' && speaking && (
        <button
          onClick={onInterrupt}
          aria-label="Interrupt Jess"
          className={`flex items-center gap-1.5 min-h-11 px-3 rounded-xl bg-brand/20 text-brand text-sm hover:bg-brand/30 transition-colors shrink-0 ${FOCUS_RING}`}
        >
          <Square size={14} className="fill-current" aria-hidden />
          Interrupt
        </button>
      )}
      <button
        onClick={onEnd}
        aria-label="End hands-free call"
        className={`flex items-center gap-1.5 min-h-11 px-3 rounded-xl bg-danger/20 text-danger text-sm hover:bg-danger/30 transition-colors shrink-0 ${FOCUS_RING}`}
      >
        <PhoneOff size={16} aria-hidden />
        End
      </button>
    </div>
  );
}
