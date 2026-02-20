import type { ChatMessage, RoutingInfo } from './types';

export async function streamChat(
  messages: ChatMessage[],
  onChunk: (text: string) => void,
  onDone: (routing?: RoutingInfo) => void,
  onError: (error: Error) => void,
): Promise<void> {
  try {
    const res = await fetch('/api/proxy/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'jessica',
        messages,
        stream: true,
      }),
    });

    if (!res.ok) {
      throw new Error(`Chat API ${res.status}: ${res.statusText}`);
    }

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let routing: RoutingInfo | undefined;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop()!;

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') {
          onDone(routing);
          return;
        }
        try {
          const parsed = JSON.parse(data);
          const content = parsed.choices?.[0]?.delta?.content;
          if (content) onChunk(content);
          if (parsed._routing) routing = parsed._routing;
        } catch {
          // skip malformed chunks
        }
      }
    }

    onDone(routing);
  } catch (err) {
    onError(err instanceof Error ? err : new Error(String(err)));
  }
}
