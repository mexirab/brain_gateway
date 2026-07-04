import { describe, it, expect } from 'vitest';
import { parsePrimaryModel } from './model-name';
import { PRIMARY_MODEL_FALLBACK } from './constants';

describe('parsePrimaryModel', () => {
  it('extracts the parenthesized model name from the /health primary field', () => {
    expect(parsePrimaryModel('http://10.0.0.195:8000 (Qwen3-30B-A3B)')).toBe('Qwen3-30B-A3B');
  });

  it('handles trailing whitespace and inner spaces', () => {
    expect(parsePrimaryModel('http://host:8000 (My Model v2)  ')).toBe('My Model v2');
  });

  it('falls back when the field is undefined', () => {
    expect(parsePrimaryModel(undefined)).toBe(PRIMARY_MODEL_FALLBACK);
  });

  it('falls back when there is no parenthesized name', () => {
    expect(parsePrimaryModel('http://host:8000')).toBe(PRIMARY_MODEL_FALLBACK);
  });
});
