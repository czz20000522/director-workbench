let expectedUser = '';
export function bindSessionUser(username: string) { expectedUser = username; }

const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  const scoped = url.origin === location.origin && url.pathname.startsWith('/api/') && url.pathname !== '/api/auth/login' && url.pathname !== '/api/auth/config';
  const owner = expectedUser;
  if (scoped && owner) {
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set('X-Workspace-User', owner);
    init = { ...init, headers };
  }
  const response = await originalFetch(input, init);
  if (scoped && owner && response.status === 401) window.dispatchEvent(new Event('workspace-session-expired'));
  if (scoped && owner !== expectedUser) throw new DOMException('用户已切换', 'AbortError');
  return response;
};
