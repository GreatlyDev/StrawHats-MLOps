# Assistant input and workspace context

## Conversation input

Assistant messages must contain at least one non-whitespace character. Whitespace-only content is rejected for both user requests and assistant history with a 422 validation response before the assistant provider is called. Valid message content is passed through unchanged, including surrounding whitespace and Markdown. Content remains limited to 1–4000 characters; a conversation contains 1–12 messages and must end with a user message.

Invalid assistant requests do not create assistant-success events. These checks validate request shape only; they do not change provider behavior or conversation history semantics.
