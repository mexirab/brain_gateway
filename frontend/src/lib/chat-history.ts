import type { ChatContentPart, ChatMessage } from './types';

/** What the chat page needs to know about an earlier bubble. */
export interface HistoryEntry {
  role: 'user' | 'assistant';
  content: string;
  /** Number of photos sent with it (photos themselves are not resent). */
  photoCount?: number;
  announcementType?: string;
  error?: boolean;
}

/**
 * Text stand-in for a message's photos, used in the saved conversation and in
 * the history resent on later turns. Photos go to the model only with the
 * turn they were attached to: the orchestrator runs vision on every image in
 * the request, so resending them would re-analyse old photos each turn.
 */
export function withPhotoMarker(text: string, photoCount: number): string {
  if (!photoCount) return text;
  const marker = photoCount === 1 ? '[Shared a photo]' : `[Shared ${photoCount} photos]`;
  return text ? `${marker} ${text}` : marker;
}

/** The current user turn: plain text, or OpenAI parts when photos are attached. */
export function userTurnContent(text: string, images: string[]): ChatMessage['content'] {
  if (!images.length) return text;
  return [
    ...(text ? [{ type: 'text', text } as ChatContentPart] : []),
    ...images.map((url): ChatContentPart => ({ type: 'image_url', image_url: { url } })),
  ];
}

/**
 * Request history: earlier bubbles as text (photos → marker, announcements
 * labelled), skipping error and blank turns — an empty assistant turn made
 * the brain reply with nothing again — then the new user turn.
 */
export function buildChatHistory(prior: HistoryEntry[], text: string, images: string[]): ChatMessage[] {
  return [
    ...prior
      .filter((m) => !m.error && (m.content.trim() || m.photoCount))
      .map((m) => ({
        role: m.role,
        content: m.announcementType
          ? `[Jess announced - ${m.announcementType}]: ${m.content}`
          : withPhotoMarker(m.content, m.photoCount ?? 0),
      })),
    { role: 'user', content: userTurnContent(text, images) },
  ];
}
