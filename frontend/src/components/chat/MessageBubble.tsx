'use client';

import { User, Bot, Bell, Volume2, Play, MoonStar, AlertCircle } from 'lucide-react';
import type { RoutingInfo, AnnouncementEntry } from '@/lib/types';
import type { SpeechStatus } from '@/lib/speech-queue';
import RoutingBadge from './RoutingBadge';

const ANNOUNCEMENT_STYLES: Record<string, { border: string; bg: string }> = {
  reminder: { border: 'border-l-warning', bg: 'bg-warning/5' },
  calendar: { border: 'border-l-info', bg: 'bg-info/5' },
  briefing: { border: 'border-l-success', bg: 'bg-success/5' },
  focus: { border: 'border-l-brand', bg: 'bg-brand/5' },
  routine: { border: 'border-l-success', bg: 'bg-success/5' },
  selfcare: { border: 'border-l-brand', bg: 'bg-brand/5' },
  email: { border: 'border-l-info', bg: 'bg-info/5' },
  progress: { border: 'border-l-warning', bg: 'bg-warning/5' },
  ambient: { border: 'border-l-content-secondary', bg: 'bg-surface-overlay/5' },
  temperature: { border: 'border-l-danger', bg: 'bg-danger/5' },
  finance: { border: 'border-l-success', bg: 'bg-success/5' },
};
const DEFAULT_STYLE = { border: 'border-l-brand', bg: 'bg-brand/5' };

interface MessageBubbleProps {
  role: 'user' | 'assistant';
  content: string;
  routing?: RoutingInfo;
  announcement?: AnnouncementEntry;
  isStreaming?: boolean;
  /** Photos sent with this message (data URIs; current session only). */
  images?: string[];
  /** Speech status for THIS message (undefined when it isn't the one speaking). */
  speechStatus?: SpeechStatus;
  /** Speak this message. Must be wired to a tap (iOS audio unlock). */
  onSpeak?: () => void;
}

// `label` is the tap target once the reply has finished; `streaming` is the
// non-interactive text shown while it is still arriving (nothing to tap yet).
const SPEECH_NOTICE: Partial<Record<SpeechStatus, { icon: typeof Play; label: string; streaming: string }>> = {
  blocked: { icon: Play, label: 'Tap to play', streaming: 'Voice blocked — play it when the reply finishes' },
  asleep: { icon: MoonStar, label: 'Jess’s voice is asleep — tap to retry', streaming: 'Jess’s voice is asleep' },
  interrupted: { icon: Play, label: 'Voice paused — tap to replay', streaming: 'Voice paused' },
  error: { icon: AlertCircle, label: 'Couldn’t play voice — tap to retry', streaming: 'Couldn’t play voice' },
};

const FOCUS_RING = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50';

export default function MessageBubble({
  role,
  content,
  routing,
  announcement,
  isStreaming,
  images,
  speechStatus,
  onSpeak,
}: MessageBubbleProps) {
  const isUser = role === 'user';
  const notice = speechStatus ? SPEECH_NOTICE[speechStatus] : undefined;
  const busy = speechStatus === 'loading' || speechStatus === 'speaking';
  const canSpeak = !isUser && !isStreaming && !!onSpeak && content.trim().length > 0;
  const isAnnouncement = !!announcement;
  const annStyle = isAnnouncement
    ? ANNOUNCEMENT_STYLES[announcement.announcement_type] ?? DEFAULT_STYLE
    : null;

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div
          className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 mt-1 ${
            isAnnouncement ? 'bg-warning/20' : 'bg-brand/20'
          }`}
        >
          {isAnnouncement ? (
            <Bell size={16} className="text-warning" />
          ) : (
            <Bot size={16} className="text-brand" />
          )}
        </div>
      )}

      <div className={`max-w-[80%] space-y-1 ${isUser ? 'items-end' : 'items-start'}`}>
        {images && images.length > 0 && (
          <div className={`flex flex-wrap gap-2 ${isUser ? 'justify-end' : ''}`}>
            {images.map((src, i) => (
              // eslint-disable-next-line @next/next/no-img-element -- data URI, nothing to optimise
              <img
                key={i}
                src={src}
                alt={`Photo ${i + 1} you sent`}
                className="h-32 max-w-full rounded-xl object-cover border border-ink/30"
              />
            ))}
          </div>
        )}
        {(content || !images?.length) && (
        <div
          className={`px-4 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
            isUser
              ? 'bg-brand/20 text-content-primary rounded-br-md'
              : isAnnouncement
                ? `${annStyle!.bg} text-content-primary border border-ink/30 border-l-2 ${annStyle!.border} rounded-bl-md`
                : 'bg-surface-raised/60 text-content-primary border border-ink/30 rounded-bl-md'
          }`}
        >
          {content}
          {isStreaming && (
            <span className="inline-block w-1.5 h-4 bg-brand rounded-full ml-0.5 animate-pulse" />
          )}
        </div>
        )}
        {isAnnouncement && (
          <span className="text-[10px] text-content-muted px-1">
            {announcement.announcement_type}
          </span>
        )}
        {notice && !isUser && isStreaming && (
          <span className="flex items-center gap-1.5 px-1 text-xs text-content-muted">
            <notice.icon size={14} aria-hidden />
            {notice.streaming}
          </span>
        )}
        {canSpeak && (notice ? (
          <button
            onClick={onSpeak}
            className={`flex items-center gap-1.5 min-h-11 px-3 rounded-lg text-xs bg-surface-overlay/40 text-content-secondary hover:text-content-primary transition-colors ${FOCUS_RING}`}
          >
            <notice.icon size={14} aria-hidden />
            {notice.label}
          </button>
        ) : (
          <button
            onClick={onSpeak}
            disabled={busy}
            aria-label={busy ? 'Speaking' : 'Play this message aloud'}
            title={busy ? 'Speaking…' : 'Play aloud'}
            className={`-my-1.5 -ml-1.5 inline-flex items-center justify-center min-h-11 min-w-11 rounded-lg transition-colors ${FOCUS_RING} ${
              busy ? 'text-brand animate-pulse' : 'text-content-muted hover:text-content-primary'
            }`}
          >
            <Volume2 size={14} aria-hidden />
          </button>
        ))}
        {routing && <RoutingBadge routing={routing} />}
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-full bg-surface-overlay/50 flex items-center justify-center shrink-0 mt-1">
          <User size={16} className="text-content-secondary" />
        </div>
      )}
    </div>
  );
}
