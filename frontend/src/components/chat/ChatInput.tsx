'use client';

import { useState, useRef, useEffect } from 'react';
import { Send, Mic, Loader2, AudioLines, ImagePlus, X } from 'lucide-react';
import useVoiceRecorder from '@/hooks/useVoiceRecorder';
import { preparePhoto, MAX_CHAT_PHOTOS } from '@/lib/chat-photos';

export type SendSource = 'text' | 'voice';

interface ChatInputProps {
  /** `images` are small JPEG data URIs (already resized, EXIF stripped). */
  onSend: (text: string, source: SendSource, images: string[]) => void;
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
  const [photos, setPhotos] = useState<string[]>([]);
  const [preparing, setPreparing] = useState(false);
  const [photoError, setPhotoError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const {
    isRecording, isTranscribing, startRecording, stopRecording, error: micError, clearError: clearMicError,
  } = useVoiceRecorder();

  const handlePhotosPicked = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ''; // allow picking the same photo again
    if (!files.length) return;
    setPhotoError(null);
    const room = MAX_CHAT_PHOTOS - photos.length;
    if (files.length > room) {
      setPhotoError(`Up to ${MAX_CHAT_PHOTOS} photos per message.`);
    }
    setPreparing(true);
    try {
      // One at a time: parallel decodes of 12 MP photos exhaust iOS memory.
      for (const file of files.slice(0, Math.max(room, 0))) {
        try {
          const uri = await preparePhoto(file);
          setPhotos((prev) => (prev.length < MAX_CHAT_PHOTOS ? [...prev, uri] : prev));
        } catch {
          setPhotoError('Couldn’t read that photo. Try a different one.');
        }
      }
    } finally {
      setPreparing(false);
    }
  };

  const removePhoto = (index: number) => {
    setPhotos((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = () => {
    const trimmed = text.trim();
    if ((!trimmed && !photos.length) || disabled || preparing) return;
    onUserGesture?.();
    clearMicError();
    setPhotoError(null);
    onSend(trimmed, 'text', photos);
    setText('');
    setPhotos([]);
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
        // A spoken "what's this?" goes out with any photos waiting above.
        onSend(result.trim(), 'voice', photos);
        setPhotos([]);
      }
    } else {
      onRecordStart?.();
      await startRecording();
    }
  };

  const micDisabled = disabled || isTranscribing;

  return (
    <div>
      {(photos.length > 0 || preparing) && (
        <div className="flex items-center gap-2 mb-2 overflow-x-auto" aria-label="Photos to send">
          {photos.map((src, i) => (
            <div key={i} className="relative shrink-0">
              {/* eslint-disable-next-line @next/next/no-img-element -- data URI preview */}
              <img src={src} alt={`Photo ${i + 1} to send`} className="h-16 w-16 rounded-lg object-cover border border-line/50" />
              <button
                onClick={() => removePhoto(i)}
                className="absolute -top-2 -right-2 inline-flex items-center justify-center h-7 w-7 rounded-full bg-surface-overlay text-content-primary border border-line/50 hover:bg-danger/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                aria-label={`Remove photo ${i + 1}`}
              >
                <X size={14} aria-hidden />
              </button>
            </div>
          ))}
          {preparing && (
            <div className="h-16 w-16 shrink-0 rounded-lg border border-line/50 flex items-center justify-center text-content-muted" role="status" aria-label="Preparing photo">
              <Loader2 size={18} className="animate-spin" aria-hidden />
            </div>
          )}
        </div>
      )}
      <div className="flex items-end gap-2">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={handlePhotosPicked}
          aria-hidden
          tabIndex={-1}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || isRecording || preparing || photos.length >= MAX_CHAT_PHOTOS}
          className="inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl transition-colors shrink-0 bg-surface-overlay/30 text-content-secondary hover:bg-surface-overlay/50 hover:text-white disabled:opacity-30"
          title="Take or attach a photo"
          aria-label="Take or attach a photo"
        >
          <ImagePlus size={18} />
        </button>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder={isRecording ? 'Listening...' : photos.length ? 'Ask about the photo…' : 'Message Jess...'}
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
          disabled={disabled || preparing || (!text.trim() && !photos.length)}
          className="inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl bg-brand/20 text-brand hover:bg-brand/30 transition-colors disabled:opacity-30 shrink-0"
          aria-label="Send message"
        >
          <Send size={18} />
        </button>
      </div>
      {photoError && (
        <div role="alert" className="flex items-center justify-between gap-2 mt-2 px-3 py-1 rounded-lg bg-surface-overlay/40 text-xs text-danger">
          <span>{photoError}</span>
          <button
            onClick={() => setPhotoError(null)}
            className="min-h-11 px-2 text-content-muted hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 rounded"
          >
            Dismiss
          </button>
        </div>
      )}
      {micError && !isRecording && (
        <div role="alert" className="flex items-center justify-between gap-2 mt-2 px-3 py-1 rounded-lg bg-surface-overlay/40 text-xs text-danger">
          <span>{micError}</span>
          <button
            onClick={clearMicError}
            className="min-h-11 px-2 text-content-muted hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 rounded"
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}
