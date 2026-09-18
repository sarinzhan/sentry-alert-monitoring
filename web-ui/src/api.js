// '/admin-web/' from vite's `base` — API calls stay under the same prefix so
// one host-nginx `location /admin-web/` covers the whole app.
// Auth: the session lives in an HttpOnly cookie set by /api/auth/login; on any
// 401 (expired/removed session) we broadcast 'auth-expired' so App.jsx can
// drop to the login screen.
const BASE = import.meta.env.BASE_URL

function unauthorized() {
  window.dispatchEvent(new Event('auth-expired'))
}

async function req(method, path, body) {
  const r = await fetch(`${BASE}api/${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (r.status === 401 && path !== 'auth/login') unauthorized()
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    const e = new Error(data.error || `Ошибка ${r.status}`)
    e.limitReached = !!data.limit_reached   // daily LLM quota -> token prompt
    throw e
  }
  return data
}

const get = (path) => req('GET', path)
const post = (path, body) => req('POST', path, body)

export const getMeta = () => get('meta')
export const investigate = (body) => post('investigate', body)
export const explain = (body) => post('explain', body)

// SSE over fetch: POST the form, feed each `data: {...}` event to onEvent as
// the model works (status / text / tool / tool_result / done / error)
async function stream(path, body, onEvent) {
  const r = await fetch(`${BASE}api/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok || !r.body) {
    if (r.status === 401) unauthorized()
    const data = await r.json().catch(() => ({}))
    const e = new Error(data.error || `Ошибка ${r.status}`)
    e.limitReached = !!data.limit_reached
    throw e
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

export const explainStream = (body, onEvent) =>
  stream('explain/stream', body, onEvent)
// free-form question (manager/admin): system prompt + question only
export const askStream = (body, onEvent) => stream('ask/stream', body, onEvent)

// --- LLM chat (manager/admin): one conversation = one resumed SDK session,
// so the model keeps the whole exchange in context between messages ---
export const getChats = () => get('chats').then((d) => d.chats)
export const getChat = (id) => get(`chats/${id}`)
export const deleteChat = (id) => req('DELETE', `chats/${id}`)
// {question, chat_id?, model?}; the done event carries chat_id
export const chatMessageStream = (body, onEvent) =>
  stream('chats/message', body, onEvent)

export const getHistory = () => get('history').then((d) => d.requests)
// per-day LLM usage split shared/personal token; username — admin only
export const getUsage = (period, username) =>
  get(`usage?period=${period}${username ? `&username=${encodeURIComponent(username)}` : ''}`)
export const getProjects = () => get('projects').then((d) => d.projects)
export const saveProject = (id, body) =>
  req('PUT', `projects/${encodeURIComponent(id)}`, body)

// --- auth ---
export const login = (username, password) => post('auth/login', { username, password })
export const logout = () => post('auth/logout', {})
// personal Claude token (used automatically after the daily quota); '' clears
export const setToken = (token) => post('auth/token', { token })
// which token analyses run on: true — personal, false — shared (with limits)
export const setTokenMode = (useOwn) => post('auth/token/mode', { use_own: useOwn })
// null = not logged in (the only 401 that is a normal answer, not an error)
export async function getMe() {
  const r = await fetch(`${BASE}api/auth/me`)
  if (r.status === 401) return null
  if (!r.ok) throw new Error(`auth: ${r.status}`)
  return r.json()
}

// --- prepared prompts (role/problem presets) + runtime settings ---
export const getPrompts = () => get('prompts')
export const createPrompt = (body) => post('prompts', body)
export const savePrompt = (id, body) => req('PUT', `prompts/${id}`, body)
export const deletePrompt = (id) => req('DELETE', `prompts/${id}`)
export const getSettings = () => get('settings')
export const saveSettings = (body) => req('PUT', 'settings', body)

// --- user management (admin) ---
export const getUsers = () => get('users').then((d) => d.users)
export const createUser = (body) => post('users', body)
export const saveUser = (id, body) => req('PUT', `users/${id}`, body)
export const deleteUser = (id) => req('DELETE', `users/${id}`)
export const getUserHistory = (id) =>
  get(`users/${id}/history`).then((d) => d.requests)
