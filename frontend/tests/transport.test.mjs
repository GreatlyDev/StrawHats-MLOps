import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { createRequest } from '../src/transport.ts';

async function serverFor(t, handler) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return createRequest(`http://127.0.0.1:${server.address().port}/api`);
}

test('sends JSON to the actual HTTP boundary and returns the response', async (t) => {
  const request = await serverFor(t, async (req, res) => {
    assert.equal(req.url, '/api/predict');
    assert.equal(req.method, 'POST');
    assert.equal(req.headers['content-type'], 'application/json');
    let body = '';
    for await (const chunk of req) body += chunk;
    assert.deepEqual(JSON.parse(body), { features: [5.1, 3.5, 1.4, 0.2] });
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ label: 'setosa' }));
  });
  assert.deepEqual(
    await request('/predict', { features: [5.1, 3.5, 1.4, 0.2] }),
    { label: 'setosa' },
  );
});

test('turns API field validation errors into actionable messages', async (t) => {
  const request = await serverFor(t, (req, res) => {
    res.writeHead(422, { 'Content-Type': 'application/json' });
    res.end(
      JSON.stringify({
        detail: [{ loc: ['body', 'replicas'], msg: 'Must be at least 1' }],
      }),
    );
  });
  await assert.rejects(
    request('/deployment-plans', { replicas: 0 }),
    /replicas: Must be at least 1/,
  );
});

test('rejects unreadable success responses rather than returning fabricated data', async (t) => {
  const request = await serverFor(t, (req, res) =>
    res.end('<html>wrong backend</html>'),
  );
  await assert.rejects(request('/overview'), /unreadable response/);
});
