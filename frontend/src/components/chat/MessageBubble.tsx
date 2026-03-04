'use client';

import { User, Bot } from 'lucide-react';
import type { RoutingInfo } from '@/lib/types';
import RoutingBadge from './RoutingBadge';

interface MessageBubbleProps {
  role: 'user' | 'assistant';
  content: string;
  routing?: RoutingInfo;
  isStreaming?: boolean;
}

export default function MessageBubble({
  role,
  content,
  routing,
  isStreaming,
}: MessageBubbleProps) {
  const isUser = role === 'user';

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-full bg-indigo-500/20 flex items-center justify-center shrink-0 mt-1">
          <Bot size={16} className="text-indigo-400" />
        </div>
      )}

      <div className={`max-w-[80%] space-y-1 ${isUser ? 'items-end' : 'items-start'}`}>
        <div
          className={`px-4 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
            isUser
              ? 'bg-indigo-500/20 text-white rounded-br-md'
              : 'bg-zinc-800/60 text-zinc-200 border border-zinc-700/30 rounded-bl-md'
          }`}
        >
          {content}
          {isStreaming && (
            <span className="inline-block w-1.5 h-4 bg-indigo-400 rounded-full ml-0.5 animate-pulse" />
          )}
        </div>
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
