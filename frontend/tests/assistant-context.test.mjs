import assert from 'node:assert/strict';
import test from 'node:test';
import { assistantContext } from '../src/assistant-context.ts';

test('a seventh exchange still fits the server contract and retains complete recent turns', () => {
  const history = Array.from({ length: 7 }, (_, index) => [
    { role: 'user', content: `Question ${index}` },
    { role: 'assistant', content: `Answer ${index}` },
  ]).flat();
  const context = assistantContext(history, 'New question');
  assert.ok(context.length <= 12);
  assert.equal(context[0].role, 'user');
  assert.deepEqual(context.at(-1), { role: 'user', content: 'New question' });
  assert.ok(context.some((message) => message.content === 'Answer 6'));
});

test('rejects messages beyond the backend limit before adding them to chat history', () => {
  assert.throws(() => assistantContext([], 'a'.repeat(4001)), /4,000/);
  assert.throws(() => assistantContext([], '   '), /question/);
});
