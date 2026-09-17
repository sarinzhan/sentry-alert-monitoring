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
