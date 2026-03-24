'use client';

import { User, Bot, Bell } from 'lucide-react';
import type { RoutingInfo, AnnouncementEntry } from '@/lib/types';
import RoutingBadge from './RoutingBadge';

const ANNOUNCEMENT_STYLES: Record<string, { border: string; bg: string }> = {
  reminder: { border: 'border-l-amber-400', bg: 'bg-amber-500/5' },
  calendar: { border: 'border-l-blue-400', bg: 'bg-blue-500/5' },
  briefing: { border: 'border-l-emerald-400', bg: 'bg-emerald-500/5' },
  focus: { border: 'border-l-purple-400', bg: 'bg-purple-500/5' },
  routine: { border: 'border-l-teal-400', bg: 'bg-teal-500/5' },
  selfcare: { border: 'border-l-pink-400', bg: 'bg-pink-500/5' },
  email: { border: 'border-l-cyan-400', bg: 'bg-cyan-500/5' },
  progress: { border: 'border-l-amber-400', bg: 'bg-amber-500/5' },
  ambient: { border: 'border-l-zinc-400', bg: 'bg-zinc-500/5' },
  temperature: { border: 'border-l-red-400', bg: 'bg-red-500/5' },
  finance: { border: 'border-l-green-400', bg: 'bg-green-500/5' },
};
const DEFAULT_STYLE = { border: 'border-l-indigo-400', bg: 'bg-indigo-500/5' };

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
            isAnnouncement ? 'bg-amber-500/20' : 'bg-indigo-500/20'
          }`}
        >
          {isAnnouncement ? (
            <Bell size={16} className="text-amber-400" />
          ) : (
            <Bot size={16} className="text-indigo-400" />
          )}
        </div>
      )}

      <div className={`max-w-[80%] space-y-1 ${isUser ? 'items-end' : 'items-start'}`}>
        <div
          className={`px-4 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
            isUser
              ? 'bg-indigo-500/20 text-white rounded-br-md'
              : isAnnouncement
                ? `${annStyle!.bg} text-zinc-200 border border-zinc-700/30 border-l-2 ${annStyle!.border} rounded-bl-md`
                : 'bg-zinc-800/60 text-zinc-200 border border-zinc-700/30 rounded-bl-md'
          }`}
        >
          {content}
          {isStreaming && (
            <span className="inline-block w-1.5 h-4 bg-indigo-400 rounded-full ml-0.5 animate-pulse" />
          )}
        </div>
        {isAnnouncement && (
          <span className="text-[10px] text-zinc-500 px-1">
            {announcement.announcement_type}
          </span>
        )}
        {routing && <RoutingBadge routing={routing} />}
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-full bg-zinc-700/50 flex items-center justify-center shrink-0 mt-1">
          <User size={16} className="text-zinc-400" />
        </div>
      )}
    </div>
  );
}
