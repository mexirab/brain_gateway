'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { MessageSquare, Volume2, VolumeX } from 'lucide-react';
import { streamChat } from '@/lib/chat';
import { api } from '@/lib/api';
import type { ChatMessage, RoutingInfo, AnnouncementEntry, Conversation } from '@/lib/types';
import MessageBubble from '@/components/chat/MessageBubble';
import ChatInput from '@/components/chat/ChatInput';
import ChatSidebar from '@/components/chat/ChatSidebar';
import useTTSPlayback from '@/hooks/useTTSPlayback';

interface DisplayMessage {
  role: 'user' | 'assistant';
  content: string;
  routing?: RoutingInfo;
  announcement?: AnnouncementEntry;
  saved?: boolean; // already persisted to DB
}

export default function ChatPage() {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastSeenIdRef = useRef<number | null>(null);
  const initRef = useRef(false);
  const streamingRef = useRef(false);
  const activeConvIdRef = useRef<string | null>(null);
  const { isSpeaking, ttsEnabled, setTtsEnabled, speak } = useTTSPlayback();
  const ttsEnabledRef = useRef(false);
  const messagesRef = useRef<DisplayMessage[]>([]);

  useEffect(() => { ttsEnabledRef.current = ttsEnabled; }, [ttsEnabled]);
  useEffect(() => { activeConvIdRef.current = activeConvId; }, [activeConvId]);
  useEffect(() => { messagesRef.current = messages; }, [messages]);

  const scrollToBottom = useCallback(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

  // Load conversation list on mount
  useEffect(() => {
    api.listConversations().then(setConversations).catch(() => {});
  }, []);

  // Load messages when switching conversations
  const loadConversation = useCallback(async (convId: string) => {
    try {
      const { messages: saved } = await api.getConversationMessages(convId);
      const display: DisplayMessage[] = saved.map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
        routing: m.routing ? JSON.parse(m.routing) : undefined,
        announcement: m.announcement_type
          ? ({ announcement_type: m.announcement_type, text: m.content } as unknown as AnnouncementEntry)
          : undefined,
        saved: true,
      }));
      setMessages(display);
      setActiveConvId(convId);
      // Seed announcement watermark from latest
      initRef.current = false;
    } catch { /* ignore */ }
  }, []);

  // Create new conversation
  const createNewChat = useCallback(async () => {
    setMessages([]);
    setActiveConvId(null);
    initRef.current = false;
  }, []);

  // Delete conversation
  const handleDelete = useCallback(async (convId: string) => {
    await api.deleteConversation(convId);
    setConversations((prev) => prev.filter((c) => c.id !== convId));
    if (activeConvId === convId) {
      setMessages([]);
      setActiveConvId(null);
    }
  }, [activeConvId]);

  // Ensure a conversation exists (create on first message if needed)
  const ensureConversation = useCallback(async (firstMessage: string): Promise<string> => {
    if (activeConvIdRef.current) return activeConvIdRef.current;
    const title = firstMessage.length > 50 ? firstMessage.slice(0, 50) + '...' : firstMessage;
    const conv = await api.createConversation(title);
    setActiveConvId(conv.id);
    setConversations((prev) => [conv, ...prev]);
    return conv.id;
  }, []);

  // Poll for new announcements
  useEffect(() => {
    const poll = async () => {
      if (streamingRef.current) return;
      try {
        const history = await api.announcementHistory(10);
        if (!history.length) return;

        if (!initRef.current) {
          lastSeenIdRef.current = Math.max(...history.map((a) => a.id));
          initRef.current = true;
          return;
        }

        const newAnnouncements = history
          .filter((a) => a.id > (lastSeenIdRef.current ?? 0) && a.success === 1)
          .sort((a, b) => a.id - b.id);

        if (newAnnouncements.length > 0) {
          const newMsgs: DisplayMessage[] = newAnnouncements.map((a) => ({
            role: 'assistant' as const,
            content: a.text,
            announcement: a,
          }));
          setMessages((prev) => [...prev, ...newMsgs]);
          lastSeenIdRef.current = Math.max(...newAnnouncements.map((a) => a.id));

          // Save announcement messages to conversation if one is active
          const convId = activeConvIdRef.current;
          if (convId) {
            for (const a of newAnnouncements) {
              api.saveMessage(convId, 'assistant', a.text, undefined, a.announcement_type).catch(() => {});
            }
          }
        }
      } catch { /* silent */ }
    };

    poll();
    const interval = setInterval(poll, 15_000);
    return () => clearInterval(interval);
  }, []);

  const handleSend = async (text: string) => {
    const userMsg: DisplayMessage = { role: 'user', content: text };
    const assistantMsg: DisplayMessage = { role: 'assistant', content: '' };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setStreaming(true);
    streamingRef.current = true;

    // Ensure conversation exists
    const convId = await ensureConversation(text);

    // Persist user message
    api.saveMessage(convId, 'user', text).catch(() => {});

    // Use ref to avoid stale closure — messages may change between click and here
    const chatHistory: ChatMessage[] = [
      ...messagesRef.current.map((m) => ({
        role: m.role,
        content: m.announcement
          ? `[Jess announced - ${m.announcement.announcement_type}]: ${m.content}`
          : m.content,
      })),
      { role: 'user' as const, content: text },
    ];

    let finalContent = '';

    await streamChat(
      chatHistory,
      (chunk) => {
        finalContent += chunk;
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          updated[updated.length - 1] = { ...last, content: last.content + chunk };
          return updated;
        });
      },
      (routing) => {
        setStreaming(false);
        streamingRef.current = false;
        if (routing) {
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = { ...updated[updated.length - 1], routing };
            return updated;
          });
        }
        // Persist assistant response
        if (finalContent.trim()) {
          api.saveMessage(convId, 'assistant', finalContent, routing).catch(() => {});
        }
        // Auto-play TTS
        if (ttsEnabledRef.current && finalContent.trim()) {
          speak(finalContent);
        }
        // Refresh conversation list (updated_at changed)
        api.listConversations().then(setConversations).catch(() => {});
      },
      (error) => {
        setStreaming(false);
        streamingRef.current = false;
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
    <div className="h-full flex" style={{ height: 'calc(100vh - 3rem)' }}>
      {/* Conversation sidebar */}
      <div className="hidden md:block">
        <ChatSidebar
          conversations={conversations}
          activeId={activeConvId}
          onSelect={loadConversation}
          onNew={createNewChat}
          onDelete={handleDelete}
        />
      </div>

      {/* Chat area */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center justify-between px-4 py-3">
          <h1 className="text-xl font-bold flex items-center gap-2">
            <MessageSquare size={20} className="text-indigo-400" />
            {activeConvId
              ? conversations.find((c) => c.id === activeConvId)?.title || 'Chat'
              : 'New Chat'}
          </h1>
          <button
            onClick={() => setTtsEnabled(!ttsEnabled)}
            className={`p-2 rounded-lg transition-colors ${
              ttsEnabled
                ? 'bg-indigo-500/20 text-indigo-400'
                : 'bg-zinc-700/30 text-zinc-500 hover:text-zinc-300'
            }`}
            title={ttsEnabled ? 'Disable voice responses' : 'Enable voice responses'}
          >
            {ttsEnabled ? <Volume2 size={18} /> : <VolumeX size={18} />}
          </button>
        </div>

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-4 px-4 pb-4">
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
              announcement={msg.announcement}
              isStreaming={streaming && i === messages.length - 1 && msg.role === 'assistant'}
            />
          ))}
        </div>

        {/* Input */}
        <div className="px-4 pb-4">
          <ChatInput onSend={handleSend} disabled={streaming || isSpeaking} />
        </div>
      </div>
    </div>
  );
}
