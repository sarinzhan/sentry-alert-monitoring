import { useEffect, useState } from 'react'
import { getProjects, saveProject } from './api.js'

function Row({ project, onSaved }) {
  const [name, setName] = useState(project.name || '')
  const [repo, setRepo] = useState(project.gitlab_repo || '')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('')

  const dirty = name !== (project.name || '') || repo !== (project.gitlab_repo || '')

  async function save() {
    setSaving(true)
    setStatus('')
    try {
      const row = await saveProject(project.id, { name, gitlab_repo: repo })
      onSaved(row)
      setStatus('✓ сохранено')
      setTimeout(() => setStatus(''), 2000)
    } catch (e) {
      setStatus(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <tr>
      <td className="ts">{project.id}</td>
      <td>
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="имя проекта" />
      </td>
      <td>
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="namespace/repo или числовой id" />
      </td>
      <td className="ts">
        <button onClick={save} disabled={!dirty || saving}>
          {saving ? '…' : 'Сохранить'}
        </button>
        <span className="hint status">{status}</span>
      </td>
    </tr>
  )
}

export default function ProjectsPanel() {
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getProjects().then(setProjects).catch((e) => setError(e.message))
  }, [])

  function onSaved(row) {
    setProjects((list) => list.map((p) => (p.id === row.id ? row : p)))
  }

  if (error) return <div className="error">{error}</div>
  if (!projects) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <p className="sub">
        Каталог проектов Sentry: имя показывается в алертах, ссылка на GitLab
        даёт ИИ доступ к коду сервиса (полный путь <code>namespace/repo</code>{' '}
        или числовой id проекта). Новые проекты появляются здесь автоматически
        по мере поступления событий.
      </p>
      <table>
        <thead>
          <tr><th>ID</th><th>Имя</th><th>GitLab репозиторий</th><th></th></tr>
        </thead>
        <tbody>
          {projects.map((p) => <Row key={p.id} project={p} onSaved={onSaved} />)}
        </tbody>
      </table>
      {projects.length === 0 && (
        <div className="empty">Пока пусто — проекты появятся с первыми событиями.</div>
      )}
    </div>
  )
}
