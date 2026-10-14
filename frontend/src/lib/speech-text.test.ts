import { describe, it, expect } from 'vitest';
import {
  cleanForSpeech, SpeechSegmenter, segmentForSpeech, SPEECH_TRUNCATED_TAIL,
  SPEECH_MAX_CHARS, SPEECH_MAX_SEGMENT_CHARS, splitLong,
} from './speech-text';

describe('cleanForSpeech', () => {
  it('drops fenced code, URLs and images; keeps link labels and inline code text', () => {
    const md = 'Run `npm test` then see [the docs](https://example.com/x).\n```bash\nrm -rf /\n```\n![pic](a.png) Also https://example.org/page';
    expect(cleanForSpeech(md)).toBe('Run npm test then see the docs. Also');
  });

  it('strips headings, emphasis, list markers and table pipes', () => {
    const md = '## Plan\n- **Buy** milk\n2. _Call_ the shop\n| a | b |\n|---|---|';
    expect(cleanForSpeech(md)).toBe('Plan Buy milk Call the shop, a, b');
  });

  it('drops an unterminated code fence (still streaming)', () => {
    expect(cleanForSpeech('Here:\n```py\nprint(1)')).toBe('Here:');
  });
});

describe('SpeechSegmenter', () => {
  it('emits a sentence only once it is followed by whitespace', () => {
    const s = new SpeechSegmenter();
    expect(s.push('Sure thing, I can help.')).toEqual([]);
    expect(s.push(' Next')).toEqual(['Sure thing, I can help.']);
    expect(s.push(' part here.')).toEqual([]);
    expect(s.flush()).toEqual(['Next part here.']);
  });

  it('does not split numbers or numbered-list markers', () => {
    const s = new SpeechSegmenter();
    expect(s.push('It costs 3.50 today. ')).toEqual(['It costs 3.50 today.']);
    expect(segmentForSpeech('1. Eggs\n2. Bread\n')).toEqual(['Eggs Bread']);
  });

  it('merges very short segments with the next one', () => {
    expect(segmentForSpeech('Ok. That reminder is set for noon. ')).toEqual([
      'Ok. That reminder is set for noon.',
    ]);
  });

  it('holds text inside an open code fence until it closes', () => {
    const s = new SpeechSegmenter();
    expect(s.push('Try this one first. ```js\nfoo(). bar(). ')).toEqual(['Try this one first.']);
    expect(s.push('\n``` And then reload it. ')).toEqual(['And then reload it.']);
  });

  it('caps the spoken total and appends the tail exactly once', () => {
    const sentence = 'This is a fairly ordinary sentence of moderate length. ';
    const out = segmentForSpeech(sentence.repeat(40), 200);
    expect(out[out.length - 1]).toBe(SPEECH_TRUNCATED_TAIL);
    expect(out.filter((t) => t === SPEECH_TRUNCATED_TAIL)).toHaveLength(1);
    const spoken = out.slice(0, -1).reduce((n, s) => n + s.length, 0);
    expect(spoken).toBeLessThanOrEqual(201); // budget + a trailing '…'
    expect(spoken).toBeGreaterThan(150);
  });

  it('cuts one giant sentence at a word boundary', () => {
    const out = segmentForSpeech('word '.repeat(300).trim() + '.', 50);
    expect(out).toHaveLength(2);
    expect(out[0].endsWith('word…')).toBe(true);
    expect(out[0].length).toBeLessThanOrEqual(51);
  });

  it('splits a whole reply into one segment per sentence (replay / buffered reply)', () => {
    expect(segmentForSpeech('First thing to do. Second thing here! Third one, finally?')).toEqual([
      'First thing to do.', 'Second thing here!', 'Third one, finally?',
    ]);
  });

  it('never speaks text from inside a closed code block', () => {
    const out = segmentForSpeech('Run this now. ```js\nfoo(). bar().\n``` Then reload the page. ');
    expect(out).toEqual(['Run this now.', 'Then reload the page.']);
  });

  it('returns nothing for markup-only replies', () => {
    expect(segmentForSpeech('```\ncode\n```')).toEqual([]);
  });
});

describe('cleanForSpeech emoji', () => {
  it('drops emoji, including ZWJ sequences and skin tones', () => {
    expect(cleanForSpeech('Nice work 🎉👍🏽 see you 👩‍💻 soon ✅')).toBe('Nice work see you soon');
  });

  it('drops lone surrogates (cannot be UTF-8 encoded for the TTS request)', () => {
    expect(cleanForSpeech('half \ud83d pair \udc4d here')).toBe('half pair here');
  });
});


describe('speech budget and long sentences', () => {
  it('speaks up to 1200 characters by default (same as Telegram)', () => {
    expect(SPEECH_MAX_CHARS).toBe(1200);
    const sentence = 'This is a fairly ordinary sentence of moderate length. ';
    const out = segmentForSpeech(sentence.repeat(40));
    const spoken = out.slice(0, -1).reduce((n, s) => n + s.length, 0);
    expect(spoken).toBeGreaterThan(1000);
    expect(spoken).toBeLessThanOrEqual(1201);
    expect(out.at(-1)).toBe(SPEECH_TRUNCATED_TAIL);
  });

  it('never emits a segment longer than the per-request limit', () => {
    const runOn = Array.from({ length: 120 }, (_, i) => `clause number ${i}, and then`).join(' ') + '.';
    const out = segmentForSpeech(runOn);
    expect(out.length).toBeGreaterThan(2);
    for (const seg of out) expect(seg.length).toBeLessThanOrEqual(SPEECH_MAX_SEGMENT_CHARS + 1);
  });

  it('splitLong prefers punctuation, then spaces, and keeps every word', () => {
    const text = 'alpha beta, gamma delta epsilon zeta eta theta iota';
    const parts = splitLong(text, 20);
    expect(parts[0]).toBe('alpha beta,');
    expect(parts.join(' ').split(/\s+/)).toEqual(text.split(/\s+/));
    expect(splitLong('short')).toEqual(['short']);
  });
});
