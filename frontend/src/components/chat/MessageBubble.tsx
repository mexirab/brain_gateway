'use client';

import { User, Bot, Bell } from 'lucide-react';
import type { RoutingInfo, AnnouncementEntry } from '@/lib/types';
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
}

export default function MessageBubble({
  role,
  content,
  routing,
  announcement,
  isStreaming,
}: MessageBubbleProps) {
  const isUser = role === 'user';
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
