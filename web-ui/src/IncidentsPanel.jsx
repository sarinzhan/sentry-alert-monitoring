import { useEffect, useState } from 'react'
import { getIncidents, getIncident } from './api.js'

// Read-only view of grouped incidents: a list (open first) with an expandable
// detail per row showing the root-cause description, every grouped issue, and
// how it was resolved. Mirrors the Telegram incident message + /incidents.

function fmtTime(sec) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString('ru')
}

// the Sentry title is often a giant exception dump; pull out the human part —
// the error class + its `detail='…'` message when present, else the first line.
function readableTitle(t) {
  if (!t) return '—'
  const detail = t.match(/detail='([^']+)'/)
  if (detail) {
    const cls = t.split(/[:{(]/)[0].trim()
    return `${cls}: ${detail[1]}`
  }
  const head = t.split('\n')[0].trim()
  return head.length > 160 ? head.slice(0, 160) + '…' : head
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0))
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d) return `${d}д ${h}ч`
  if (h) return `${h}ч ${m}м`
  if (m) return `${m}м`
  return `${sec}с`
}

function StatusBadge({ status }) {
  const open = status === 'open'
  return (
    <span className="badge" style={{ background: open ? '#b45309' : '#15803d' }}>
      {open ? 'открыт' : 'разрешён'}
    </span>
  )
}

function resolvedBy(inc) {
  if (inc.status !== 'resolved') return null
  const who = inc.resolved_by ? `@${inc.resolved_by}`
    : (inc.resolved_kind === 'auto' ? 'автоматически' : 'вручную')
  const unver = inc.resolved_kind === 'unverified' ? ' (без подтверждения)' : ''
  return `${who}${unver}`
}

function Detail({ id }) {
  const [inc, setInc] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getIncident(id).then((d) => alive && setInc(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [id])

  if (error) return <div className="error">{error}</div>
  if (!inc) return <div className="hint">Загружаю…</div>

  const dur = (inc.resolved_at || inc.last_member_at) - inc.opened_at
  return (
    <div className="incident-detail">
      {inc.description && (
        <div className="field wide">
          <label><b>Причина (AI)</b></label>
          <div className="explanation-text">{inc.description.replace(/^🤖\s*/, '')}</div>
        </div>
      )}
      <div className="field wide">
        <label><b>Связанные ошибки ({inc.members?.length || 0})</b></label>
        <table className="tbl">
          <thead>
            <tr><th>#</th><th>Проект</th><th>Заголовок</th><th>События</th><th>Добавлена</th></tr>
          </thead>
          <tbody>
            {(inc.members || []).map((m) => (
              <tr key={m.issue_id}>
                <td>
                  {m.url
                    ? <a href={m.url} target="_blank" rel="noreferrer"><code>#{m.short || '—'}</code></a>
                    : <code>#{m.short || '—'}</code>}
                </td>
                <td>{m.project || '—'}</td>
                <td title={m.title || ''}>{readableTitle(m.title)}</td>
                <td className="hint">
                  {m.events ?? '—'}{m.users ? ` · ${m.users}👤` : ''}
                </td>
                <td className="hint">{fmtTime(m.joined_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="field wide">
        <div className="hint">
          Открыт: {fmtTime(inc.opened_at)} · последняя ошибка: {fmtTime(inc.last_member_at)}
          {' '}· длительность: {fmtDur(dur)}
          {inc.status === 'resolved' && (
            <> · разрешён: {fmtTime(inc.resolved_at)} · {resolvedBy(inc)}</>
          )}
          {' '}· доставлен в {inc.messages?.length || 0} чат(ов)
        </div>
        {inc.resolution && (
          <div className="explanation-text"><b>Решение:</b> {inc.resolution}</div>
        )}
      </div>
    </div>
  )
}

export default function IncidentsPanel({ active }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')          // '' | 'open' | 'resolved'
  const [open, setOpen] = useState(null)            // expanded incident_id

  async function load() {
    try { setData(await getIncidents(filter)) }
    catch (e) { setError(e.message) }
  }

  useEffect(() => { if (active) load() }, [active, filter])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="empty">Загружаю…</div>

  const filters = [['', 'Все'], ['open', 'Открытые'], ['resolved', 'Разрешённые']]

  return (
    <div>
      <p className="sub">
        Инциденты — группы связанных ошибок с одной причиной. Одно сообщение на
        инцидент уходит в подписанные чаты; здесь видна вся группа и как её закрыли.
        {!data.group_enabled && ' ⚠️ Группировка выключена (GROUP_ENABLED=false).'}
      </p>
      <div className="actions">
        {filters.map(([v, label]) => (
          <button key={v} type="button" className={filter === v ? 'tab active' : 'tab'}
                  onClick={() => setFilter(v)}>{label}</button>
        ))}
        <button type="button" className="tab" onClick={load}>Обновить</button>
      </div>

      {data.incidents.length === 0 && <div className="empty">Инцидентов нет.</div>}

      {data.incidents.map((inc) => (
        <div className="results tg-chat" key={inc.incident_id}>
          <h2 style={{ cursor: 'pointer' }}
              onClick={() => setOpen(open === inc.incident_id ? null : inc.incident_id)}>
            {open === inc.incident_id ? '▾' : '▸'}{' '}
            <code>#{inc.incident_id}</code> {inc.title || 'Инцидент'}{' '}
            <StatusBadge status={inc.status} />
          </h2>
          <div className="hint">
            {inc.member_count} ошибок · проекты: {inc.projects || '—'} ·
            {' '}активность: {fmtTime(inc.last_member_at)}
            {inc.status === 'resolved' && <> · {resolvedBy(inc)}</>}
          </div>
          {open === inc.incident_id && <Detail id={inc.incident_id} />}
        </div>
      ))}
    </div>
  )
}
