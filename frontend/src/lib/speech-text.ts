/**
 * Text preparation for spoken chat replies: turn streamed markdown into
 * speakable sentences, as early as possible, within a total length budget.
 *
 * Pure (no DOM / React) so it is unit-testable; used by useTTSPlayback.
 */

/**
 * Total characters spoken per reply before "the rest is on screen". Same as
 * Telegram's voice replies (TELEGRAM_VOICE_REPLY_MAX_CHARS default 1200).
 */
export const SPEECH_MAX_CHARS = 1200;

/**
 * Longest single TTS request. /api/tts/synthesize rejects > 1000 chars, and a
 * shorter clip starts playing sooner, so run-on sentences are split.
 */
export const SPEECH_MAX_SEGMENT_CHARS = 400;
export const SPEECH_TRUNCATED_TAIL = 'The rest is on screen.';

// Segments shorter than this are held and merged with the next one, so a
// list marker or "Ok." doesn't cost a whole TTS round-trip on its own.
const MIN_SEGMENT_CHARS = 12;

/** Strip markdown, code and URLs so TTS reads only the prose. */
export function cleanForSpeech(raw: string): string {
  let t = raw;
  t = t.replace(/```[\s\S]*?(```|$)/g, ' '); // fenced code (closed or still streaming)
  t = t.replace(/`([^`]*)`/g, '$1'); // inline code → its text
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, ' '); // images
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1'); // [label](url) → label
  t = t.replace(/<?\b(?:https?:\/\/|www\.)[^\s<>]+>?/gi, ' '); // bare URLs
  t = t.replace(/<\/?[a-z][^>]*>/gi, ' '); // stray HTML tags
  t = t.replace(/^\s{0,3}#{1,6}\s+/gm, ''); // headings
  t = t.replace(/^\s{0,3}>\s?/gm, ''); // blockquotes
  t = t.replace(/^\s*(?:[-*+•]|\d{1,3}[.)])\s+/gm, ''); // list markers
  t = t.replace(/^\s*\|?\s*:?-{3,}.*$/gm, ' '); // table separator rows
  t = t.replace(/\|/g, ', '); // table cells
  t = t.replace(/(\*\*|__|~~)(.+?)\1/g, '$2'); // bold / strike
  t = t.replace(/(^|[^\w*])[*_]([^*_\n]+)[*_](?=[^\w*]|$)/g, '$1$2'); // italics
  t = t.replace(/[*_~#]{2,}/g, ' '); // leftover emphasis runs
  t = t.replace(/(?:\p{Extended_Pictographic}|\p{Regional_Indicator})[\u{FE0F}\u{200D}\p{Emoji_Modifier}]*/gu, ' '); // emoji
  t = t.replace(/\p{Cs}/gu, ''); // lone surrogates — not valid UTF-8, server 400s them
  t = t.replace(/\s+/g, ' ').trim();
  t = t.replace(/\s+([,.!?;:])/g, '$1').replace(/(,\s*){2,}/g, ', ');
  return t.replace(/^[,\s]+|[,\s]+$/g, '');
}

// A boundary is terminal punctuation (not after a digit, so "1." / "3.5"
// don't split) followed by whitespace, or a newline. Requiring trailing
// whitespace means a boundary at the very end of a still-streaming buffer is
// never taken early.
const BOUNDARY = /(?:[^\d\s][.!?…]+["')\]]*\s+|\n+)/g;

/** End offsets of every sentence boundary in `s`. */
function boundaries(s: string): number[] {
  return Array.from(s.matchAll(BOUNDARY), (m) => (m.index ?? 0) + m[0].length);
}

/**
 * Same-length copy of `s` with closed ``` blocks blanked to "x" (so no
 * boundary is ever found inside code) and cut at an unclosed fence (code
 * still streaming — nothing after it is ready).
 */
function maskCode(s: string): string {
  let out = '';
  let i = 0;
  for (;;) {
    const open = s.indexOf('```', i);
    if (open < 0) return out + s.slice(i);
    const close = s.indexOf('```', open + 3);
    if (close < 0) return out + s.slice(i, open);
    out += s.slice(i, open) + 'x'.repeat(close + 3 - open);
    i = close + 3;
  }
}

/**
 * Incrementally splits streamed reply text into speakable segments and
 * enforces the total budget. `push()` returns segments that are complete;
 * `flush()` returns whatever remains once the stream has finished.
 */
export class SpeechSegmenter {
  private buf = '';
  private pending = ''; // cleaned text held back for being too short
  private spoken = 0;
  private exhausted = false;

  constructor(private readonly maxChars: number = SPEECH_MAX_CHARS) {}

  push(chunk: string): string[] {
    if (this.exhausted) return [];
    this.buf += chunk;
    const ends = boundaries(maskCode(this.buf));
    if (ends.length === 0) return [];
    const ready = this.buf;
    this.buf = ready.slice(ends[ends.length - 1]);
    return ends.flatMap((end, k) => this.emit(ready.slice(k ? ends[k - 1] : 0, end), false));
  }

  flush(): string[] {
    if (this.exhausted) return [];
    const rest = this.buf;
    this.buf = '';
    const ends = boundaries(maskCode(rest)).filter((e) => e < rest.length);
    const cuts = [0, ...ends, rest.length];
    const out = cuts.slice(1).flatMap((end, k) => this.emit(rest.slice(cuts[k], end), false));
    return [...out, ...this.emit('', true)];
  }

  private emit(raw: string, final: boolean): string[] {
    if (this.exhausted) return [];
    const cleaned = cleanForSpeech(raw);
    const text = [this.pending, cleaned].filter(Boolean).join(' ');
    if (!text) return [];
    if (!final && text.length < MIN_SEGMENT_CHARS) {
      this.pending = text;
      return [];
    }
    this.pending = '';
    const room = this.maxChars - this.spoken;
    if (text.length <= room) {
      this.spoken += text.length;
      return splitLong(text);
    }
    // Over budget: speak what fits (cut at a word boundary), then the tail.
    this.exhausted = true;
    const out: string[] = [];
    if (room > MIN_SEGMENT_CHARS) {
      const cut = text.slice(0, room);
      const sentenceEnd = boundaries(cut + ' ').at(-1) ?? -1;
      const head = sentenceEnd > MIN_SEGMENT_CHARS
        ? cut.slice(0, sentenceEnd).trim()
        : cut.slice(0, Math.max(cut.lastIndexOf(' '), 1)).trim() + '…';
      out.push(...splitLong(head));
    }
    out.push(SPEECH_TRUNCATED_TAIL);
    return out;
  }
}

/** Split text longer than `max` at the last comma/semicolon/dash, else space. */
export function splitLong(text: string, max: number = SPEECH_MAX_SEGMENT_CHARS): string[] {
  const out: string[] = [];
  let rest = text.trim();
  while (rest.length > max) {
    const window = rest.slice(0, max);
    const punct = Math.max(window.lastIndexOf(', '), window.lastIndexOf('; '), window.lastIndexOf(' — '));
    const cut = punct > max / 3 ? punct + 1 : window.lastIndexOf(' ') > 0 ? window.lastIndexOf(' ') : max;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out;
}

/** Whole-text convenience (replay / tap-to-play): all segments at once. */
export function segmentForSpeech(text: string, maxChars: number = SPEECH_MAX_CHARS): string[] {
  const s = new SpeechSegmenter(maxChars);
  return [...s.push(text), ...s.flush()];
}
