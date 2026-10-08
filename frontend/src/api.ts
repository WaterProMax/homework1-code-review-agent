export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, body === undefined ? undefined : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : '请求参数无效'; } catch { /* preserve status */ }
    throw new Error(message);
  }
  return response.json();
}
export function downloadText(name: string, value: string) {
  const url = URL.createObjectURL(new Blob([value], {type:'text/plain;charset=utf-8'}));
  const anchor = document.createElement('a'); anchor.href=url; anchor.download=name; anchor.click(); URL.revokeObjectURL(url);
}
