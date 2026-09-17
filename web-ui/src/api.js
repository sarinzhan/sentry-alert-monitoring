// '/admin-web/' from vite's `base` — API calls stay under the same prefix so
// one host-nginx `location /admin-web/` covers the whole app
const BASE = import.meta.env.BASE_URL

export async function getMeta() {
  const r = await fetch(`${BASE}api/meta`)
  if (!r.ok) throw new Error(`meta: ${r.status}`)
  return r.json()
}

async function post(path, body) {
  const r = await fetch(`${BASE}api/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`)
  return data
}

export const investigate = (body) => post('investigate', body)
export const explain = (body) => post('explain', body)

// SSE over fetch: POST the form, feed each `data: {...}` event to onEvent as
// the model works (status / text / tool / tool_result / done / error)
export async function explainStream(body, onEvent) {
  const r = await fetch(`${BASE}api/explain/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok || !r.body) {
    const data = await r.json().catch(() => ({}))
    throw new Error(data.error || `Ошибка ${r.status}`)
  }
  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let sep
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)))
      }
    }
  }
}

export async function getProjects() {
  const r = await fetch(`${BASE}api/projects`)
  if (!r.ok) throw new Error(`projects: ${r.status}`)
  return (await r.json()).projects
}

export async function saveProject(id, body) {
  const r = await fetch(`${BASE}api/projects/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`)
  return data
}
