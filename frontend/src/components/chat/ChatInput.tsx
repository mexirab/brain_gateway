'use client';

import { useState, useRef, useEffect } from 'react';
import { Send, Mic, Loader2, AudioLines, ImagePlus, X } from 'lucide-react';
import useVoiceRecorder from '@/hooks/useVoiceRecorder';
import { preparePhoto, MAX_CHAT_PHOTOS, PhotoTooLargeError } from '@/lib/chat-photos';

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

interface PendingPhoto {
  id: number;
  uri: string;
}

let photoIdCounter = 0;

const FOCUS_RING = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50';
const ICON_BUTTON = `inline-flex items-center justify-center min-h-11 min-w-11 rounded-xl transition-colors shrink-0 ${FOCUS_RING}`;
const QUIET_BUTTON = `${ICON_BUTTON} bg-surface-overlay/30 text-content-secondary hover:bg-surface-overlay/50 hover:text-white disabled:opacity-30`;

export default function ChatInput({
  onSend, disabled, onUserGesture, onRecordStart, onStartCall, focusCallButton,
}: ChatInputProps) {
  const callButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focusCallButton) callButtonRef.current?.focus();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- mount only
  const [text, setText] = useState('');
  const [photos, setPhotos] = useState<PendingPhoto[]>([]);
  // The mic path reads photos after a multi-second transcription await; the
  // ref gives it the tray as it is THEN, not as it was at the stop tap.
  const photosRef = useRef<PendingPhoto[]>([]);
  useEffect(() => { photosRef.current = photos; }, [photos]);
  const [preparing, setPreparing] = useState(false);
  const [photoError, setPhotoError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const photoButtonRef = useRef<HTMLButtonElement>(null);
  const {
    isRecording, isTranscribing, startRecording, stopRecording, error: micError, clearError: clearMicError,
  } = useVoiceRecorder();

  const handlePhotosPicked = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ''; // allow picking the same photo again
    if (!files.length) return;
    setPhotoError(null);
    const room = MAX_CHAT_PHOTOS - photosRef.current.length;
    if (files.length > room) {
      setPhotoError(`Up to ${MAX_CHAT_PHOTOS} photos per message.`);
    }
    setPreparing(true);
    try {
      // One at a time: parallel decodes of 12 MP photos exhaust iOS memory.
      for (const file of files.slice(0, Math.max(room, 0))) {
        try {
          const uri = await preparePhoto(file);
          setPhotos((prev) => (
            prev.length < MAX_CHAT_PHOTOS ? [...prev, { id: ++photoIdCounter, uri }] : prev
          ));
        } catch (err) {
          setPhotoError(err instanceof PhotoTooLargeError
            ? 'That photo is too detailed to send. Try a closer or simpler shot.'
            : 'Couldn’t open that photo on this device. Pick another one, or take a new one.');
        }
      }
    } finally {
      setPreparing(false);
    }
  };

  const removePhoto = (id: number) => {
    const next = photosRef.current.filter((p) => p.id !== id);
    setPhotos(next);
    // The focused X just unmounted; don't drop keyboard/VoiceOver users on <body>.
    if (!next.length) photoButtonRef.current?.focus();
  };

  const takePhotos = (): string[] => {
    const uris = photosRef.current.map((p) => p.uri);
    photosRef.current = [];
    setPhotos([]);
    return uris;
  };

  const handleSubmit = () => {
    const trimmed = text.trim();
    if ((!trimmed && !photos.length) || disabled || preparing) return;
    onUserGesture?.();
    clearMicError();
    setPhotoError(null);
    onSend(trimmed, 'text', takePhotos());
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
        // A spoken "what's this?" goes out with the photos waiting above.
        onSend(result.trim(), 'voice', takePhotos());
      }
    } else {
      onRecordStart?.();
      await startRecording();
    }
  };

  const hasDraft = text.trim().length > 0 || photos.length > 0;
  const busyMic = isRecording || isTranscribing;
  // Waiting for a photo to be prepared would otherwise send without it.
  const micDisabled = disabled || isTranscribing || (preparing && !isRecording);
  const photosFull = photos.length >= MAX_CHAT_PHOTOS;
  const photoLabel = photosFull ? `Up to ${MAX_CHAT_PHOTOS} photos per message` : 'Take or attach a photo';

  return (
    <div>
      {(photos.length > 0 || preparing) && (
        // pt/pr leave room for the remove buttons: overflow-x-auto clips y too.
        <div role="group" aria-label="Photos to send" className="flex items-center gap-3 mb-1 pt-3 pr-3 overflow-x-auto">
          {photos.map((p, i) => (
            <div key={p.id} className="relative shrink-0">
              {/* eslint-disable-next-line @next/next/no-img-element -- data URI preview */}
              <img src={p.uri} alt={`Photo ${i + 1} to send`} className="h-16 w-16 rounded-lg object-cover border border-line/50" />
              <button
                onClick={() => removePhoto(p.id)}
                disabled={busyMic}
                className={`group absolute -top-3 -right-3 inline-flex items-center justify-center h-11 w-11 rounded-full disabled:opacity-40 ${FOCUS_RING}`}
                aria-label={`Remove photo ${i + 1}`}
              >
                <span className="inline-flex items-center justify-center h-7 w-7 rounded-full bg-surface-overlay text-content-primary border border-line/50 group-hover:bg-danger/40">
                  <X size={14} aria-hidden />
                </span>
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
        />
        <button
          ref={photoButtonRef}
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || busyMic || preparing || photosFull}
          className={QUIET_BUTTON}
          title={photoLabel}
          aria-label={photoLabel}
        >
          <ImagePlus size={18} />
        </button>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder={isRecording ? 'Listening...' : photos.length ? 'Ask about it…' : 'Message Jess...'}
          disabled={disabled || isRecording}
          rows={1}
          className="flex-1 min-w-0 px-3 py-2.5 bg-surface-raised/60 border border-line/50 rounded-xl text-sm text-white placeholder-content-muted focus:outline-none focus:border-brand/50 resize-none disabled:opacity-50"
        />
        <button
          onClick={handleMicClick}
          disabled={micDisabled}
          className={`${ICON_BUTTON} ${
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
        {/* One slot keeps the row narrow on a phone: Send while there is
            something to send, otherwise the hands-free call button. Hiding
            the call button with photos waiting also stops call mode from
            unmounting this input and silently dropping them. */}
        {hasDraft || !onStartCall ? (
          <button
            onClick={handleSubmit}
            disabled={disabled || preparing || !hasDraft}
            className={`${ICON_BUTTON} bg-brand/20 text-brand hover:bg-brand/30 disabled:opacity-30`}
            aria-label="Send message"
          >
            <Send size={18} />
          </button>
        ) : (
          <button
            ref={callButtonRef}
            onClick={() => { onUserGesture?.(); onStartCall(); }}
            disabled={disabled || busyMic}
            className={QUIET_BUTTON}
            title="Hands-free conversation"
            aria-label="Start hands-free conversation"
          >
            <AudioLines size={18} />
          </button>
        )}
      </div>
      {photoError && (
        <div role="alert" className="flex items-center justify-between gap-2 mt-2 px-3 py-1 rounded-lg bg-surface-overlay/40 text-sm text-danger">
          <span>{photoError}</span>
          <button
            onClick={() => setPhotoError(null)}
            className={`min-h-11 px-2 text-content-muted hover:text-content-primary rounded ${FOCUS_RING}`}
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
            className={`min-h-11 px-2 text-content-muted hover:text-content-primary rounded ${FOCUS_RING}`}
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}
