async function request(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${options?.method || 'GET'} ${path} -> ${res.status} ${body.slice(0, 200)}`)
  }
  return res.json()
}

export const getFlows = () => request('/api/flows')
export const getTests = () => request('/api/tests')
export const getRun = (id) => request(`/api/runs/${id}`)
export const getRuns = () => request('/api/runs')

export const startRun = (nodeIds) =>
  request('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_ids: nodeIds }),
  })

export const updateStep = (flowSlug, stepSlug, patch) =>
  request(`/api/flows/${flowSlug}/steps/${stepSlug}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
