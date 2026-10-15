'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import useSWR from 'swr';
import { MessageSquare, Volume2, VolumeX, PanelLeftClose, PanelLeftOpen, Square } from 'lucide-react';
import { streamChat } from '@/lib/chat';
import { api } from '@/lib/api';
import { friendlyError } from '@/lib/errors';
import type { ChatMessage, RoutingInfo, AnnouncementEntry, Conversation } from '@/lib/types';
import MessageBubble from '@/components/chat/MessageBubble';
import ChatInput, { type SendSource } from '@/components/chat/ChatInput';
import ChatSidebar from '@/components/chat/ChatSidebar';
import useTTSPlayback from '@/hooks/useTTSPlayback';
import useVoiceCall from '@/hooks/useVoiceCall';
import VoiceCallBar from '@/components/chat/VoiceCallBar';

interface DisplayMessage {
  /** Client-side id: React key + which bubble a speech status belongs to. */
  uid: string;
  role: 'user' | 'assistant';
  content: string;
  routing?: RoutingInfo;
  announcement?: AnnouncementEntry;
  saved?: boolean; // already persisted to DB
  error?: boolean; // friendlyError placeholder — not worth speaking
}

// Screen-reader text for the always-mounted live region (a region mounted
// together with its text is often not announced).
const SPEECH_ANNOUNCE: Record<string, string> = {
  blocked: 'Voice reply blocked by the browser. Use Tap to play.',
  asleep: 'Jess’s voice is asleep.',
  interrupted: 'Voice reply paused.',
  error: 'Couldn’t play the voice reply.',
};

const SIDEBAR_PREF_KEY = 'chat:sidebar-open';

let uidCounter = 0;
const nextUid = () => `m${++uidCounter}`;

export default function ChatPage() {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarReady, setSidebarReady] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastSeenIdRef = useRef<number | null>(null);
  const initRef = useRef(false);
  const streamingRef = useRef(false);
  const activeConvIdRef = useRef<string | null>(null);
  const {
    ttsEnabled, setTtsEnabled, speech, isSpeaking, unlock,
    beginReply, pushText, endReply, speakText, stop: stopSpeech,
  } = useTTSPlayback();
  const ttsEnabledRef = useRef(false);
  // Set after useVoiceCall below; lets earlier callbacks end a call.
  const callEndRef = useRef<() => void>(() => {});
  const messagesRef = useRef<DisplayMessage[]>([]);

  useEffect(() => { ttsEnabledRef.current = ttsEnabled; }, [ttsEnabled]);
  useEffect(() => { activeConvIdRef.current = activeConvId; }, [activeConvId]);
  useEffect(() => { messagesRef.current = messages; }, [messages]);

  // Sidebar default: persisted preference, falling back to "open if the
  // viewport is large enough that the rails won't crowd the chat" — closed
  // on landscape phone (short height + sub-laptop width).
  useEffect(() => {
    let initial: boolean;
    try {
      const stored = localStorage.getItem(SIDEBAR_PREF_KEY);
      if (stored === '1' || stored === '0') {
        initial = stored === '1';
      } else {
        initial = window.matchMedia('(min-width: 768px) and (min-height: 600px)').matches;
      }
    } catch {
      initial = false;
    }
    setSidebarOpen(initial);
    setSidebarReady(true);
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebarOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_PREF_KEY, next ? '1' : '0');
      } catch {
        // ignore — preference simply won't persist
      }
      return next;
    });
  }, []);

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
      stopSpeech();
      callEndRef.current();
      const display: DisplayMessage[] = saved.map((m) => ({
        uid: nextUid(),
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
    } catch { /* ignore */ }
  }, [stopSpeech]);

  // Create new conversation
  const createNewChat = useCallback(async () => {
    stopSpeech();
    callEndRef.current();
    setMessages([]);
    setActiveConvId(null);
  }, [stopSpeech]);

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

  // Poll for new announcements. SWR pauses while a response is streaming
  // (isPaused) and automatically when the tab is hidden (refreshInterval),
  // and dedupes unchanged history so the processing effect only runs on change.
  const { data: annHistory } = useSWR(
    'chat:announcement-poll',
    () => api.announcementHistory(10),
    { refreshInterval: 15_000, isPaused: () => streamingRef.current },
  );

  useEffect(() => {
    if (!annHistory || !annHistory.length) return;

    // On first load just record the high-water mark — don't replay history.
    if (!initRef.current) {
      lastSeenIdRef.current = Math.max(...annHistory.map((a) => a.id));
      initRef.current = true;
      return;
    }

    const newAnnouncements = annHistory
      .filter((a) => a.id > (lastSeenIdRef.current ?? 0) && a.success === 1)
      .sort((a, b) => a.id - b.id);

    if (newAnnouncements.length > 0) {
      const newMsgs: DisplayMessage[] = newAnnouncements.map((a) => ({
        uid: nextUid(),
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
  }, [annHistory]);

  const handleSend = async (text: string, source: SendSource) => {
    // Snapshot the history BEFORE adding this turn: messagesRef is synced by
    // an effect, and after the await below it can already contain the new
    // user message + empty placeholder (first message of a new chat) — which
    // sent the question twice with a blank assistant turn between them.
    const prior = messagesRef.current;
    const userMsg: DisplayMessage = { uid: nextUid(), role: 'user', content: text };
    const assistantMsg: DisplayMessage = { uid: nextUid(), role: 'assistant', content: '' };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setStreaming(true);
    streamingRef.current = true;

    // Walkie-talkie: a spoken question always gets a spoken answer; typed
    // messages follow the speaker toggle. Speech starts per sentence while
    // the reply is still streaming.
    const speakReply = source === 'voice' || ttsEnabledRef.current;
    // Resolves when the spoken reply has finished (or was stopped) — call
    // mode waits on it before listening again.
    const speechDone = speakReply ? beginReply(assistantMsg.uid) : undefined;
    if (!speakReply) stopSpeech();

    // Ensure conversation exists. If that fails, finish the turn visibly
    // (otherwise `streaming` stays true and the input is dead).
    let convId: string;
    try {
      convId = await ensureConversation(text);
    } catch (err) {
      setStreaming(false);
      streamingRef.current = false;
      if (speakReply) endReply();
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          error: true,
          content: friendlyError(err, 'Sorry — I couldn’t start the conversation just now. Please try again.'),
        };
        return updated;
      });
      return;
    }

    // Persist user message
    api.saveMessage(convId, 'user', text).catch(() => {});

    // Use ref to avoid stale closure — messages may change between click and here
    // Skip blank and error bubbles: an empty assistant turn in the history
    // made the brain reply with nothing again (the server drops them too).
    const chatHistory: ChatMessage[] = [
      ...prior.filter((m) => !m.error && m.content.trim()).map((m) => ({
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
        if (speakReply) pushText(chunk);
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
        } else {
          // No text came back: say so instead of leaving a blank bubble —
          // out loud too when the reply was going to be spoken (hands-free
          // users aren't looking at the screen).
          const noReply = 'Sorry — I didn’t get a reply just now. Please try again.';
          if (speakReply) pushText(noReply);
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              error: true,
              routing: undefined,
              content: noReply,
            };
            return updated;
          });
        }
        if (speakReply) endReply();
        // Refresh conversation list (updated_at changed)
        api.listConversations().then(setConversations).catch(() => {});
      },
      (error) => {
        setStreaming(false);
        streamingRef.current = false;
        if (speakReply) endReply(); // speak whatever arrived before the error
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            error: true,
            content: friendlyError(error, 'Sorry — I couldn’t reach the model just now. Please try again.'),
          };
          return updated;
        });
      },
      { conversationId: convId },
    );
    await speechDone;
  };

  // Hands-free call: each finished utterance is sent like a mic message and
  // the call waits for the spoken reply before listening again.
  useEffect(() => { callEndRef.current = call.end; });
  const handleSendRef = useRef(handleSend);
  useEffect(() => { handleSendRef.current = handleSend; });
  const call = useVoiceCall(useCallback((text: string) => handleSendRef.current(text, 'voice'), []));
  const { end: callEnd } = call;
  const endCall = useCallback(() => { callEnd(); stopSpeech(); }, [callEnd, stopSpeech]);
  // Return focus to the call button when a call ends (the bar unmounts).
  const wasInCallRef = useRef(false);
  useEffect(() => { wasInCallRef.current = call.active; }, [call.active]);

  return (
    <div className="page-viewport flex">
      {/* Conversation sidebar — collapsible. Hidden on mobile, toggleable on md+. */}
      {sidebarReady && sidebarOpen && (
        <div className="hidden md:block">
          <ChatSidebar
            conversations={conversations}
            activeId={activeConvId}
            onSelect={loadConversation}
            onNew={createNewChat}
            onDelete={handleDelete}
          />
        </div>
      )}

      {/* Chat area */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center justify-between px-4 py-3 gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <button
              onClick={toggleSidebar}
              aria-label={sidebarOpen ? 'Hide conversation history' : 'Show conversation history'}
              aria-expanded={sidebarOpen}
              className="hidden md:inline-flex p-2 rounded-lg text-content-muted hover:text-content-primary hover:bg-surface-raised/60 transition-colors shrink-0"
              title={sidebarOpen ? 'Hide history' : 'Show history'}
            >
              {sidebarOpen ? <PanelLeftClose size={18} aria-hidden /> : <PanelLeftOpen size={18} aria-hidden />}
            </button>
            <h1 className="text-xl font-bold flex items-center gap-2 min-w-0">
              <MessageSquare size={20} className="text-brand shrink-0" />
              <span className="truncate">
                {activeConvId
                  ? conversations.find((c) => c.id === activeConvId)?.title || 'Chat'
                  : 'New Chat'}
              </span>
            </h1>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {isSpeaking && (
              <button
                onClick={stopSpeech}
                className="flex items-center gap-1.5 min-h-11 px-3 rounded-lg bg-brand/20 text-brand text-sm transition-colors hover:bg-brand/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                aria-label="Stop speaking"
              >
                <Square size={14} className="fill-current" aria-hidden />
                Stop
              </button>
            )}
            <button
              onClick={() => {
                const next = !ttsEnabled;
                if (next) unlock(); else stopSpeech();
                setTtsEnabled(next);
              }}
              aria-pressed={ttsEnabled}
              aria-label={ttsEnabled ? 'Disable voice responses' : 'Enable voice responses'}
              className={`p-2 rounded-lg transition-colors shrink-0 ${
                ttsEnabled
                  ? 'bg-brand/20 text-brand'
                  : 'bg-surface-overlay/30 text-content-muted hover:text-content-primary'
              }`}
              title={ttsEnabled ? 'Disable voice responses' : 'Enable voice responses'}
            >
              {ttsEnabled ? <Volume2 size={18} /> : <VolumeX size={18} />}
            </button>
          </div>
        </div>

        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-4 px-4 pb-4">
          {messages.length === 0 && (
            <div className="flex-1 flex items-center justify-center h-full">
              <div className="text-center text-content-muted">
                <MessageSquare size={48} className="mx-auto mb-3 opacity-30" />
                <p className="text-sm">Start a conversation with Jess</p>
                <p className="text-xs mt-1 text-content-muted">
                  Ask about your schedule, control smart home, get reminders, or just chat
                </p>
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <MessageBubble
              key={msg.uid}
              role={msg.role}
              content={msg.content}
              routing={msg.routing}
              announcement={msg.announcement}
              isStreaming={streaming && i === messages.length - 1 && msg.role === 'assistant'}
              speechStatus={speech.key === msg.uid ? speech.status : undefined}
              onSpeak={msg.error ? undefined : () => { unlock(); speakText(msg.uid, msg.content); }}
            />
          ))}
        </div>

        <span className="sr-only" role="status" aria-live="polite">
          {SPEECH_ANNOUNCE[speech.status] ?? ''}
        </span>

        {/* Input */}
        <div className="px-4 pb-4">
          {call.error && !call.active && (
            <div className="flex items-center justify-between gap-2 mb-2 px-3 py-2 rounded-lg bg-surface-overlay/40 text-xs text-content-secondary" role="alert">
              <span>{call.error}</span>
              <button
                onClick={call.clearError}
                className="min-h-11 px-2 text-content-muted hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 rounded"
              >
                Dismiss
              </button>
            </div>
          )}
          {call.active ? (
            <VoiceCallBar
              phase={call.phase}
              speaking={isSpeaking}
              onInterrupt={stopSpeech}
              onEnd={endCall}
            />
          ) : (
            <ChatInput
              onSend={handleSend}
              disabled={streaming}
              onUserGesture={unlock}
              onRecordStart={stopSpeech}
              onStartCall={() => { stopSpeech(); call.start(); }}
              focusCallButton={wasInCallRef.current}
            />
          )}
        </div>
      </div>
    </div>
  );
}
