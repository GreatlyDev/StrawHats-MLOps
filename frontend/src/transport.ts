export function createRequest(base: string) {
  return async function request<T>(
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${base}${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers:
          body === undefined
            ? undefined
            : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError')
        throw error;
      throw new Error(
        'Cannot reach the API. Start the backend or check the configured backend URL, then retry.',
      );
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail
                .map(
                  (item: { msg?: string; loc?: (string | number)[] }) =>
                    `${item.loc?.slice(1).join('.') || 'Input'}: ${item.msg || 'Invalid value'}`,
                )
                .join('; ')
            : `The API returned ${response.status}. Please retry.`,
      );
    }
    if (data === null)
      throw new Error(
        'The API returned an unreadable response. Check the backend URL and retry.',
      );
    return data as T;
  };
}
