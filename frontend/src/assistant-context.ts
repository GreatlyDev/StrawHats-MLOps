import type { Message } from './types';

export function assistantContext(history: Message[], input: string): Message[] {
  const content = input.trim();
  if (!content) throw new Error('Enter a question for the assistant.');
  if (content.length > 4000)
    throw new Error('Keep your question within 4,000 characters.');
  // Five complete previous exchanges plus this question fit the API's 12-message cap.
  return [...history.slice(-10), { role: 'user', content }];
}
