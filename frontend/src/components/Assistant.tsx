import { useState } from 'react';
import type { FormEvent } from 'react';
import { Sparkles, Send, ArrowUpRight } from 'lucide-react';
import { request } from '../api';
import { assistantContext } from '../assistant-context';
import type { Health, Message } from '../types';
import { Badge, ErrorNotice } from './shared';
const suggestions = [
  'How should I deploy my registered model?',
  'Explain my model evaluation metrics.',
  'What should I monitor after deployment?',
];
export default function Assistant({ health }: { health: Health }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!input.trim() || busy) return;
    const prompt = input.trim();
    let context: Message[];
    try {
      context = assistantContext(messages, prompt);
    } catch (error) {
      setError((error as Error).message);
      return;
    }
    const next: Message[] = [...messages, { role: 'user', content: prompt }];
    setMessages(next);
    setInput('');
    setError('');
    setBusy(true);
    try {
      const answer = await request<{ message: string; model: string }>(
        '/assistant',
        { messages: context },
      );
      setMessages([...next, { role: 'assistant', content: answer.message }]);
    } catch (error) {
      setMessages(messages);
      setInput(prompt);
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="section-toolbar">
        <div>
          <h2>A little guidance for the voyage</h2>
          <p>Ask about your models, evaluation, and deployment plans.</p>
        </div>
        <Badge tone={health.ai_configured ? 'green' : 'gold'}>
          {health.ai_configured ? health.ai_model : 'Provider not configured'}
        </Badge>
      </div>
      <div className="assistant-layout">
        <section className="card assistant-card">
          <div
            className="chat-messages"
            aria-live="polite"
            aria-relevant="additions"
          >
            {messages.length ? (
              messages.map((message, i) => (
                <div className={`message ${message.role}`} key={i}>
                  <span>
                    {message.role === 'user' ? 'You' : 'StrawHats assistant'}
                  </span>
                  <p>{message.content}</p>
                </div>
              ))
            ) : (
              <div className="assistant-welcome">
                <div className="sparkle-icon">
                  <Sparkles size={30} />
                </div>
                <h3>Think through your next step.</h3>
                <p>
                  Your assistant uses the current workspace to help you
                  understand model results and prepare deployment plans.
                </p>
                <div className="suggestions">
                  {suggestions.map((suggestion) => (
                    <button
                      key={suggestion}
                      onClick={() => setInput(suggestion)}
                    >
                      {suggestion}
                      <ArrowUpRight size={16} />
                    </button>
                  ))}
                </div>
              </div>
            )}
            {busy && (
              <div className="thinking" role="status">
                Looking at your workspace…
              </div>
            )}
          </div>
          {error && <ErrorNotice message={error} />}
          <form className="composer" onSubmit={submit}>
            <label htmlFor="assistant-input" className="sr-only">
              Message the assistant
            </label>
            <textarea
              id="assistant-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask a question about your workspace…"
              rows={2}
              required
              maxLength={4000}
              disabled={busy}
            />
            <button
              className="primary"
              disabled={busy || !input.trim() || !health.ai_configured}
              aria-label="Send message"
            >
              <Send size={18} />
            </button>
          </form>
          <small className="chat-footnote">
            Recommendations only. The assistant cannot apply cluster changes.
          </small>
        </section>
        <aside className="card assistant-aside">
          <span className="eyebrow">YOUR WORKSPACE COPILOT</span>
          <h3>
            Grounded in
            <br />
            your actual work.
          </h3>
          <p>
            Get help interpreting registered models, measured inference, and
            saved deployment previews.
          </p>
          <div className="soft-note">
            <strong>Stay in control</strong>
            <p>
              Review any recommended configuration before using it in your
              infrastructure.
            </p>
          </div>
          {!health.ai_configured && (
            <ErrorNotice message="Configure the AI provider on the backend to enable messages. API keys stay on the server." />
          )}
        </aside>
      </div>
    </>
  );
}
