import { Fragment, useRef, useState, useEffect } from 'react'
import {
  getProjects, saveProject, projectAuditStream,
  getSentryTeams, createSentryProject, inviteSentryMember,
} from './api.js'
import Reasoning from './Reasoning.jsx'

// Create a project in Sentry itself (needs project:write on the token).
function CreateProjectForm({ teams, onCreated }) {
  const [name, setName] = useState('')
  const [team, setTeam] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setMsg('')
    setErr('')
    try {
      const p = await createSentryProject({ team: team || teams[0]?.slug, name })
      setMsg(`✓ создан проект ${p.slug} (id ${p.id}) — привяжите GitLab репозиторий в таблице выше`)
      setName('')
      onCreated()
    } catch (e2) {
      setErr(e2.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="settings-block" onSubmit={submit} noValidate>
      <h2>➕ Создать проект в Sentry</h2>
      <div className="field">
        <label>Название <span className="req">*</span></label>
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="billing-service" />
      </div>
      <div className="field">
        <label>Команда</label>
        <select value={team} onChange={(e) => setTeam(e.target.value)}>
          {teams.map((t) => <option key={t.slug} value={t.slug}>{t.name}</option>)}
        </select>
      </div>
      <div className="actions">
        <button type="submit" disabled={busy || !name.trim() || !teams.length}>
          {busy ? '…' : 'Создать'}
        </button>
        {msg && <span className="hint">{msg}</span>}
      </div>
      {err && <p className="error">{err}</p>}
    </form>
  )
}

// Invite a user into the Sentry org. SMTP is not configured, so the invite
// email never arrives — we show the invite LINK to hand over manually.
function InviteMemberForm({ teams }) {
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [team, setTeam] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setResult(null)
    setErr('')
    try {
      setResult(await inviteSentryMember({ email, role, team: team || undefined }))
      setEmail('')
    } catch (e2) {
      setErr(e2.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="settings-block" onSubmit={submit} noValidate>
      <h2>➕ Пригласить пользователя в Sentry</h2>
      <div className="field">
        <label>Email <span className="req">*</span></label>
        <input value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder="user@company.kg" />
      </div>
      <div className="field">
        <label>Роль</label>
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="member">member</option>
          <option value="admin">admin</option>
          <option value="manager">manager</option>
          <option value="owner">owner</option>
        </select>
      </div>
      <div className="field">
        <label>Команда</label>
        <select value={team} onChange={(e) => setTeam(e.target.value)}>
          <option value="">—</option>
          {teams.map((t) => <option key={t.slug} value={t.slug}>{t.name}</option>)}
        </select>
      </div>
      <div className="actions">
        <button type="submit" disabled={busy || !email.includes('@')}>
          {busy ? '…' : 'Пригласить'}
        </button>
      </div>
      {err && <p className="error">{err}</p>}
      {result && (
        <div className="field wide">
          {result.invite_link ? (
            <>
              <label><b>✓ Приглашение создано.</b> Почта в Sentry не настроена —
                отправьте эту ссылку пользователю (по ней он задаст пароль):</label>
              <input readOnly value={result.invite_link}
                     onFocus={(e) => e.target.select()} />
            </>
          ) : (
            <span className="hint">
              Приглашение создано, но эта версия Sentry не отдаёт ссылку по
              API — создайте пользователя через <code>sentry createuser</code>.
            </span>
          )}
        </div>
      )}
    </form>
  )
}

function Row({ project, onSaved, audit, onAudit }) {
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
      <td className="proj">{project.slug || '—'}</td>
      <td>
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder={project.slug || 'имя проекта'} />
      </td>
      <td>
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="namespace/repo или числовой id" />
      </td>
      <td className="ts">
        <button onClick={save} disabled={!dirty || saving}>
          {saving ? '…' : 'Сохранить'}
        </button>{' '}
        <button type="button" onClick={() => onAudit(project.id)}
                disabled={audit.running}
                title="ИИ проверит, достаточно ли у сервиса логов и событий для расследования жалоб">
          {audit.running && audit.pid === project.id ? '…' : '🩺 Логи?'}
        </button>
        <span className="hint status">{status}</span>
      </td>
    </tr>
  )
}

// One expandable row under a project: the live audit stream, or the stored
// verdict of a previous run.
function AuditDetail({ project, audit, onStop }) {
  const isLive = audit.running && audit.pid === project.id
  if (!isLive && !project.audit) return null
  return (
    <tr className="hist-detail">
      <td colSpan={5}>
        {isLive ? (
          <>
            <div className="explain-bar">
              <span className="hint">проверяю наблюдаемость…</span>
              <button type="button" className="stop" onClick={onStop}>⏹ Стоп</button>
            </div>
            {audit.error && <p className="error">{audit.error}</p>}
            <Reasoning steps={audit.steps} running usage={null} live={audit.live} />
          </>
        ) : (
          <>
            <div className="msg">{project.audit}</div>
            <div className="hint">
              аудит логов ИИ — проверьте выводы
              {project.audit_at &&
                ` · ${new Date(project.audit_at * 1000).toLocaleDateString('ru')}`}
              {project.audit_llm_id && ` · ${project.audit_llm_id}`}
            </div>
          </>
        )}
      </td>
    </tr>
  )
}

export default function ProjectsPanel() {
  const [projects, setProjects] = useState(null)
  const [teams, setTeams] = useState([])
  const [error, setError] = useState('')
  const [audit, setAudit] = useState({ running: false, pid: null, steps: [], live: '', error: '' })
  const abortRef = useRef(null)

  function reload() {
    getProjects().then(setProjects).catch((e) => setError(e.message))
  }

  useEffect(() => {
    reload()
    getSentryTeams().then(setTeams).catch(() => {})  // needs org:read; forms hide without it
  }, [])

  function onSaved(row) {
    setProjects((list) => list.map((p) => (p.id === row.id ? row : p)))
  }

  async function runAudit(pid) {
    if (audit.running) return
    setAudit({ running: true, pid, steps: [], live: '', error: '' })
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let steps = []
    try {
      await projectAuditStream(pid, (ev) => {
        if (ev.type === 'done') {
          setProjects((list) => list.map((p) => p.id === pid
            ? { ...p, audit: ev.explanation, audit_at: Date.now() / 1000,
                audit_llm_id: ev.llm_id }
            : p))
        } else if (ev.type === 'error') {
          setAudit((a) => ({ ...a, error: ev.error }))
        } else if (ev.type === 'delta') {
          setAudit((a) => ({ ...a, live: a.live + ev.text }))
        } else if (ev.type !== 'usage') {
          steps = [...steps, ev]
          setAudit((a) => ({ ...a, steps, live: '' }))
        }
      }, ctrl.signal)
    } catch (e) {
      if (e.name !== 'AbortError') setError(e.message)
    } finally {
      abortRef.current = null
      setAudit({ running: false, pid: null, steps: [], live: '', error: '' })
    }
  }

  if (error && !projects) return <div className="error">{error}</div>
  if (!projects) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <p className="sub">
        Каталог проектов Sentry: имя показывается в алертах, ссылка на GitLab
        даёт ИИ доступ к коду сервиса (полный путь <code>namespace/repo</code>{' '}
        или числовой id проекта). Кнопка «🩺 Логи?» — ИИ проверяет, достаточно
        ли у сервиса логов и событий, чтобы расследовать жалобы пользователей.
      </p>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr><th>ID</th><th>Slug (Sentry)</th><th>Имя</th><th>GitLab репозиторий</th><th></th></tr>
        </thead>
        <tbody>
          {projects.map((p) => (
            <Fragment key={p.id}>
              <Row project={p} onSaved={onSaved} audit={audit} onAudit={runAudit} />
              <AuditDetail project={p} audit={audit}
                           onStop={() => abortRef.current?.abort()} />
            </Fragment>
          ))}
        </tbody>
      </table>
      {projects.length === 0 && (
        <div className="empty">Пока пусто — проекты появятся с первыми событиями.</div>
      )}
      {teams.length > 0 ? (
        <>
          <CreateProjectForm teams={teams} onCreated={reload} />
          <InviteMemberForm teams={teams} />
        </>
      ) : (
        <p className="hint">
          Создание проектов и приглашение пользователей Sentry появятся здесь,
          когда токену SENTRY_API_TOKEN добавят права: Organization Read,
          Project Write, Member Admin (Internal Integration → Permissions).
        </p>
      )}
    </div>
  )
}
