'use client';

import { useState, useRef, useCallback } from 'react';
import { pickRecorderMime, transcribe, STTUnavailableError } from '@/lib/stt';
import { setAudioSessionType } from '@/lib/audio-session';

interface UseVoiceRecorderReturn {
  isRecording: boolean;
  isTranscribing: boolean;
  startRecording: () => Promise<void>;
  stopRecording: () => Promise<string | null>;
  error: string | null;
  clearError: () => void;
}

export default function useVoiceRecorder(): UseVoiceRecorderReturn {
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const startRecording = useCallback(async () => {
    setError(null);
    try {
      // Never inherit a 'playback' session (iOS would refuse to capture).
      setAudioSessionType('auto');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const { mimeType } = pickRecorderMime();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      mediaRecorderRef.current = recorder;
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      recorder.start();
      setIsRecording(true);
    } catch (err) {
      // Release mic if acquired before the error (e.g. MediaRecorder constructor throws)
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      const name = err instanceof Error ? err.name : '';
      console.warn('[mic] start failed:', name || err);
      setError(
        !navigator.mediaDevices?.getUserMedia ? 'The microphone needs the secure (https://) dashboard address.'
          : name === 'NotAllowedError' ? 'Microphone access is blocked — allow it for this site in your browser settings, then reload.'
            : name === 'NotFoundError' ? 'No microphone found.'
              : name === 'NotReadableError' ? 'The microphone is busy in another app.'
                : name === 'NotSupportedError' ? 'Recording isn’t supported in this browser.'
                  : 'Couldn’t start the microphone — try reloading the page.',
      );
    }
  }, []);

  const stopRecording = useCallback(async (): Promise<string | null> => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === 'inactive') return null;

    // Wait for recorder to finish
    const blob = await new Promise<Blob>((resolve) => {
      recorder.onstop = () => {
        resolve(new Blob(chunksRef.current, { type: recorder.mimeType }));
      };
      recorder.stop();
    });

    // Release mic
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setIsRecording(false);

    // Send to STT
    setIsTranscribing(true);
    try {
      return (await transcribe(blob)) || null;
    } catch (err) {
      setError(err instanceof STTUnavailableError
        ? 'Jess can’t hear right now — Helios (speech recognition) is asleep.'
        : 'Couldn’t transcribe that — please try again.');
      return null;
    } finally {
      setIsTranscribing(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { isRecording, isTranscribing, startRecording, stopRecording, error, clearError };
}
