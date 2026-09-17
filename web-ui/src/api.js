export async function getMeta() {
  const r = await fetch('/api/meta')
  if (!r.ok) throw new Error(`meta: ${r.status}`)
  return r.json()
}

export async function investigate(body) {
  const r = await fetch('/api/investigate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`)
  return data
}
