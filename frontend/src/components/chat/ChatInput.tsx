'use client';

import { useState, useRef } from 'react';
import { Send, Mic, Loader2 } from 'lucide-react';
import useVoiceRecorder from '@/hooks/useVoiceRecorder';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export default function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { isRecording, isTranscribing, startRecording, stopRecording } = useVoiceRecorder();

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
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
    if (isRecording) {
      const result = await stopRecording();
      if (result?.trim()) {
        onSend(result.trim());
      }
    } else {
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
        className="flex-1 px-4 py-2.5 bg-zinc-800/60 border border-zinc-700/50 rounded-xl text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50 resize-none disabled:opacity-50"
      />
      <button
        onClick={handleMicClick}
        disabled={micDisabled}
        className={`p-2.5 rounded-xl transition-colors shrink-0 ${
          isRecording
            ? 'bg-red-500/30 text-red-400 animate-pulse'
            : 'bg-zinc-700/30 text-zinc-400 hover:bg-zinc-700/50 hover:text-white'
        } disabled:opacity-30`}
        title={isRecording ? 'Stop recording' : 'Voice input'}
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
        className="p-2.5 rounded-xl bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30 transition-colors disabled:opacity-30 shrink-0"
      >
        <Send size={18} />
      </button>
    </div>
  );
}
