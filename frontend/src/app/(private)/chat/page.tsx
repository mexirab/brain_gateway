'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { MessageSquare } from 'lucide-react';
import { streamChat } from '@/lib/chat';
import type { ChatMessage, RoutingInfo } from '@/lib/types';
import MessageBubble from '@/components/chat/MessageBubble';
import ChatInput from '@/components/chat/ChatInput';

interface DisplayMessage {
  role: 'user' | 'assistant';
  content: string;
  routing?: RoutingInfo;
}

export default function ChatPage() {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const handleSend = async (text: string) => {
    const userMsg: DisplayMessage = { role: 'user', content: text };
    const assistantMsg: DisplayMessage = { role: 'assistant', content: '' };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setStreaming(true);

    // Build message history for API
    const chatHistory: ChatMessage[] = [
      ...messages.map((m) => ({ role: m.role, content: m.content })),
      { role: 'user' as const, content: text },
    ];

    await streamChat(
      chatHistory,
      // onChunk
      (chunk) => {
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          updated[updated.length - 1] = { ...last, content: last.content + chunk };
          return updated;
        });
      },
      // onDone
      (routing) => {
        setStreaming(false);
        if (routing) {
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              routing,
            };
            return updated;
          });
        }
      },
      // onError
      (error) => {
        setStreaming(false);
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content: `Error: ${error.message}`,
          };
          return updated;
        });
      },
    );
  };

  return (
    <div className="h-full flex flex-col" style={{ height: 'calc(100vh - 3rem)' }}>
      <h1 className="text-2xl font-bold mb-4 flex items-center gap-2">
        <MessageSquare size={24} className="text-indigo-400" />
        Chat with Jess
      </h1>

      {/* Messages area */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto space-y-4 mb-4 pr-2"
      >
        {messages.length === 0 && (
          <div className="flex-1 flex items-center justify-center h-full">
            <div className="text-center text-zinc-500">
              <MessageSquare size={48} className="mx-auto mb-3 opacity-30" />
              <p className="text-sm">Start a conversation with Jess</p>
              <p className="text-xs mt-1 text-zinc-600">
                Ask about your schedule, control smart home, get reminders, or just chat
              </p>
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <MessageBubble
            key={i}
            role={msg.role}
            content={msg.content}
            routing={msg.routing}
            isStreaming={streaming && i === messages.length - 1 && msg.role === 'assistant'}
          />
        ))}
      </div>

      {/* Input */}
      <ChatInput onSend={handleSend} disabled={streaming} />
    </div>
  );
}
