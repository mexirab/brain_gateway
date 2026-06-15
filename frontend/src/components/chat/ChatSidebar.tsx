'use client';

import { Plus, Trash2, MessageSquare } from 'lucide-react';
import type { Conversation } from '@/lib/types';

interface ChatSidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  return `${days}d`;
}

export default function ChatSidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
}: ChatSidebarProps) {
  return (
    <div className="w-64 border-r border-line-subtle flex flex-col h-full bg-surface-base/50">
      <div className="p-3">
        <button
          onClick={onNew}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-brand/20 text-brand hover:bg-brand/30 transition-colors text-sm font-medium"
        >
          <Plus size={16} />
          New Chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 space-y-0.5">
        {conversations.map((conv) => (
          <div
            key={conv.id}
            className={`group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-colors text-sm ${
              conv.id === activeId
                ? 'bg-surface-overlay/50 text-white'
                : 'text-content-secondary hover:bg-surface-raised/60 hover:text-content-primary'
            }`}
            onClick={() => onSelect(conv.id)}
          >
            <MessageSquare size={14} className="shrink-0 opacity-50" />
            <span className="flex-1 truncate">{conv.title}</span>
            <span className="text-[10px] text-content-muted shrink-0">
              {timeAgo(conv.updated_at)}
            </span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete(conv.id);
              }}
              className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-danger transition-opacity shrink-0"
              title="Delete"
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}

        {conversations.length === 0 && (
          <p className="text-xs text-content-muted text-center py-8">No conversations yet</p>
        )}
      </div>
    </div>
  );
}
