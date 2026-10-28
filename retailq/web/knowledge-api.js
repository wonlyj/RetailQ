export class ConsoleError extends Error {
  constructor(code, message, status = 0) { super(message); this.code = code; this.status = status; }
}

export async function consoleRequest(path, { method = 'GET', body, signal } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-RetailQ-Console'] = '1';
  }
  let response;
  try {
    response = await fetch(`/api/knowledge${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin', cache: 'no-store', signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ConsoleError('connection_unknown', '连接中断，操作是否受理尚未确认；请查询状态或用同一请求重试。');
  }
  let data;
  try { data = await response.json(); }
  catch { throw new ConsoleError('invalid_response', '服务未返回可读取的状态，请稍后刷新。', response.status); }
  if (!response.ok) throw new ConsoleError(data.error?.code || 'invalid_request',
    data.error?.message || '输入格式无效，请检查会话、分类或版本。', response.status);
  return data;
}

export function newRequestKey() { return crypto.randomUUID().replaceAll('-', ''); }
