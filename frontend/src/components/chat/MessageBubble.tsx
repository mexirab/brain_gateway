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
  /** Speech status for THIS message (undefined when it isn't the one speaking). */
  speechStatus?: SpeechStatus;
  /** Speak this message. Must be wired to a tap (iOS audio unlock). */
  onSpeak?: () => void;
}

const SPEECH_NOTICE: Partial<Record<SpeechStatus, { icon: typeof Play; label: string }>> = {
  blocked: { icon: Play, label: 'Tap to play' },
  asleep: { icon: MoonStar, label: 'Jess’s voice is asleep — tap to retry' },
  error: { icon: AlertCircle, label: 'Couldn’t play voice — tap to retry' },
};

export default function MessageBubble({
  role,
  content,
  routing,
  announcement,
  isStreaming,
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
        <div
          className={`px-4 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
            isUser
              ? 'bg-brand/20 text-white rounded-br-md'
              : isAnnouncement
                ? `${annStyle!.bg} text-content-primary border border-line/30 border-l-2 ${annStyle!.border} rounded-bl-md`
                : 'bg-surface-raised/60 text-content-primary border border-line/30 rounded-bl-md'
          }`}
        >
          {content}
          {isStreaming && (
            <span className="inline-block w-1.5 h-4 bg-brand rounded-full ml-0.5 animate-pulse" />
          )}
        </div>
        {isAnnouncement && (
          <span className="text-[10px] text-content-muted px-1">
            {announcement.announcement_type}
          </span>
        )}
        {notice && !isUser && isStreaming && (
          <span className="flex items-center gap-1.5 px-1 text-xs text-content-muted" role="status">
            <notice.icon size={14} aria-hidden />
            {notice.label.replace(/ — tap to retry$/, '')}
          </span>
        )}
        {canSpeak && (notice ? (
          <button
            onClick={onSpeak}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs bg-surface-overlay/40 text-content-secondary hover:text-content-primary transition-colors"
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
            className={`p-1.5 rounded-lg transition-colors ${
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
