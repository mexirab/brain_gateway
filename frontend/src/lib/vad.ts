/**
 * Energy-based voice activity detection for hands-free chat ("call mode").
 *
 * Fed one RMS level per audio frame (~every 50 ms) from a Web Audio
 * AnalyserNode. Tracks the room's noise floor while nobody is talking and
 * reports when speech starts and when it has ended (a stretch of silence).
 * Deliberately simple — no ML model to download — and pure, so it is
 * unit-testable without a microphone.
 */

export interface VadConfig {
  /** Level must stay above the speech threshold this long to count as speech. */
  minSpeechMs: number;
  /** Silence after speech that ends the utterance (generous: people pause to think). */
  silenceMs: number;
  /**
   * Nobody talks this long without a `silenceMs` pause — it is steady noise
   * (TV, radio). Reported as 'speech-timeout' so the caller DISCARDS it rather
   * than sending room audio to the assistant as if the user had said it.
   */
  maxUtteranceMs: number;
  /** Absolute minimum RMS treated as speech, whatever the noise floor. */
  minRms: number;
  /** Speech threshold = max(minRms, noiseFloor × ratio). */
  ratio: number;
  /** While speaking, "still talking" uses threshold × this (hysteresis). */
  holdFactor: number;
  /** EMA weight for noise-floor updates during silence. */
  floorAlpha: number;
  /** First stretch after construction only measures the room (no events). */
  calibrationMs: number;
}

export const DEFAULT_VAD_CONFIG: VadConfig = {
  minSpeechMs: 200,
  silenceMs: 1800,
  maxUtteranceMs: 90_000,
  minRms: 0.01,
  ratio: 3,
  holdFactor: 0.6,
  floorAlpha: 0.05,
  calibrationMs: 400,
};

export type VadEvent = 'speech-start' | 'speech-end' | 'speech-timeout' | null;

export class EnergyVad {
  private readonly cfg: VadConfig;
  private floor: number;
  private speaking = false;
  private aboveSince: number | null = null;
  private startedAt = 0;
  private lastVoiceAt = 0;
  private speechAvg = 0;
  private calibStart: number | null = null;
  private calibSum = 0;
  private calibN = 0;

  constructor(config: Partial<VadConfig> = {}) {
    this.cfg = { ...DEFAULT_VAD_CONFIG, ...config };
    this.floor = this.cfg.minRms / this.cfg.ratio;
  }

  get isSpeaking(): boolean {
    return this.speaking;
  }

  /** Above threshold but not yet confirmed as speech (onset in progress). */
  get isRising(): boolean {
    return this.aboveSince !== null;
  }

  get threshold(): number {
    return Math.max(this.cfg.minRms, this.floor * this.cfg.ratio);
  }

  /** Forget the current utterance; keeps the learned noise floor. */
  reset(): void {
    this.speaking = false;
    this.aboveSince = null;
  }

  feed(rms: number, now: number): VadEvent {
    const c = this.cfg;
    // Calibrate: a room that is already noisy when the call starts must
    // become the floor, not be mistaken for the user talking.
    if (this.calibStart === null) this.calibStart = now;
    if (now - this.calibStart < c.calibrationMs) {
      this.calibSum += rms;
      this.calibN += 1;
      this.floor = this.calibSum / this.calibN;
      return null;
    }
    if (!this.speaking) {
      if (rms >= this.threshold) {
        this.aboveSince ??= now;
        if (now - this.aboveSince >= c.minSpeechMs) {
          this.speaking = true;
          this.startedAt = this.aboveSince;
          this.lastVoiceAt = now;
          this.aboveSince = null;
          this.speechAvg = rms;
          return 'speech-start';
        }
      } else {
        this.aboveSince = null;
        this.floor += c.floorAlpha * (rms - this.floor);
      }
      return null;
    }

    // Speaking: the floor is frozen so a long sentence can't raise its own bar.
    this.speechAvg += 0.05 * (rms - this.speechAvg);
    if (rms >= this.threshold * c.holdFactor) this.lastVoiceAt = now;
    if (now - this.lastVoiceAt >= c.silenceMs) {
      this.speaking = false;
      return 'speech-end';
    }
    if (now - this.startedAt >= c.maxUtteranceMs) {
      // Nobody talks non-stop for this long: it was steady noise (TV, fan).
      // Adopt its level as the floor so it doesn't immediately re-trigger.
      this.speaking = false;
      this.floor = Math.max(this.floor, this.speechAvg);
      return 'speech-timeout';
    }
    return null;
  }
}

/** RMS of a time-domain buffer (AnalyserNode.getFloatTimeDomainData). */
export function rmsOf(samples: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / (samples.length || 1));
}
