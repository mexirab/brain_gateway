'use client';

import { useState, useRef, useCallback, useEffect } from 'react';

const PROXY = '/api/proxy';
const LS_KEY = 'tts_enabled';

interface UseTTSPlaybackReturn {
  isSpeaking: boolean;
  ttsEnabled: boolean;
  setTtsEnabled: (enabled: boolean) => void;
  speak: (text: string) => Promise<void>;
  stop: () => void;
}

export default function useTTSPlayback(): UseTTSPlaybackReturn {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [ttsEnabled, setTtsEnabledState] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const blobUrlRef = useRef<string | null>(null);

  // Hydrate from localStorage after mount
  useEffect(() => {
    setTtsEnabledState(localStorage.getItem(LS_KEY) === 'true');
  }, []);

  const setTtsEnabled = useCallback((enabled: boolean) => {
    setTtsEnabledState(enabled);
    localStorage.setItem(LS_KEY, String(enabled));
  }, []);

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    if (blobUrlRef.current) {
      URL.revokeObjectURL(blobUrlRef.current);
      blobUrlRef.current = null;
    }
    setIsSpeaking(false);
  }, []);

  const speak = useCallback(async (text: string) => {
    stop();
    try {
      const res = await fetch(`${PROXY}/api/tts/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) return;

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      blobUrlRef.current = url;

      const audio = new Audio(url);
      audioRef.current = audio;
      audio.onended = () => {
        setIsSpeaking(false);
        URL.revokeObjectURL(url);
        blobUrlRef.current = null;
      };
      setIsSpeaking(true);
      await audio.play();
    } catch {
      setIsSpeaking(false);
    }
  }, [stop]);

  // Cleanup on unmount
  useEffect(() => stop, [stop]);

  return { isSpeaking, ttsEnabled, setTtsEnabled, speak, stop };
}
