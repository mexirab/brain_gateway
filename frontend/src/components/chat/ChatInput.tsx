'use client';

import { useState, useRef, useEffect } from 'react';
import { Send, Mic, Loader2, AudioLines } from 'lucide-react';
import useVoiceRecorder from '@/hooks/useVoiceRecorder';

export type SendSource = 'text' | 'voice';

interface ChatInputProps {
  onSend: (text: string, source: SendSource) => void;
  disabled?: boolean;
  /**
   * Called synchronously at the start of every send/mic tap — the only moment
   * iOS Safari lets the page unlock audio for the spoken reply.
   */
  onUserGesture?: () => void;
  /** Called when recording starts, so Jess stops talking over the user. */
  onRecordStart?: () => void;
  /** Start hands-free call mode. Called synchronously inside the tap. */
  onStartCall?: () => void;
  /** Focus the call button on mount (a call just ended). */
  focusCallButton?: boolean;
}

export default function ChatInput({
  onSend, disabled, onUserGesture, onRecordStart, onStartCall, focusCallButton,
}: ChatInputProps) {
  const callButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focusCallButton) callButtonRef.current?.focus();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- mount only
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { isRecording, isTranscribing, startRecording, stopRecording } = useVoiceRecorder();

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onUserGesture?.();
    onSend(trimmed, 'text');
    setText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  };

  const handleMicClick = async () => {
    onUserGesture?.(); // before any await — must stay inside the tap
    if (isRecording) {
      const result = await stopRecording();
      if (result?.trim()) {
        onSend(result.trim(), 'voice');
      }
    } else {
      onRecordStart?.();
      await startRecording();
    }
  };

  const micDisabled = disabled || isTranscribing;

  return (
    <div className="flex items-end gap-2">
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        onInput={handleInput}
        placeholder={isRecording ? 'Listening...' : 'Message Jess...'}
        disabled={disabled || isRecording}
        rows={1}
        className="flex-1 px-4 py-2.5 bg-surface-raised/60 border border-line/50 rounded-xl text-sm text-white placeholder-content-muted focus:outline-none focus:border-brand/50 resize-none disabled:opacity-50"
      />
      {onStartCall && (
        <button
          ref={callButtonRef}
          onClick={() => { onUserGesture?.(); onStartCall(); }}
          disabled={disabled || isRecording || isTranscribing}
          className="inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl transition-colors shrink-0 bg-surface-overlay/30 text-content-secondary hover:bg-surface-overlay/50 hover:text-white disabled:opacity-30"
          title="Hands-free conversation"
          aria-label="Start hands-free conversation"
        >
          <AudioLines size={18} />
        </button>
      )}
      <button
        onClick={handleMicClick}
        disabled={micDisabled}
        className={`inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl transition-colors shrink-0 ${
          isRecording
            ? 'bg-danger/30 text-danger animate-pulse'
            : 'bg-surface-overlay/30 text-content-secondary hover:bg-surface-overlay/50 hover:text-white'
        } disabled:opacity-30`}
        title={isRecording ? 'Stop recording' : 'Voice input'}
        aria-label={isRecording ? 'Stop recording' : 'Voice input'}
      >
        {isTranscribing ? (
          <Loader2 size={18} className="animate-spin" />
        ) : (
          <Mic size={18} />
        )}
      </button>
      <button
        onClick={handleSubmit}
        disabled={disabled || !text.trim()}
        className="inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl bg-brand/20 text-brand hover:bg-brand/30 transition-colors disabled:opacity-30 shrink-0"
        aria-label="Send message"
      >
        <Send size={18} />
      </button>
    </div>
  );
}
