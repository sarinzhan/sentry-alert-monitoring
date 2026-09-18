import { useEffect, useState } from 'react'
import { getHistory } from './api.js'

const ts = (at) => new Date(at * 1000).toLocaleString('ru-RU')

function idents(r) {
  return [r.request_id && `req: ${r.request_id}`,
          r.device_id && `dev: ${r.device_id}`,
          r.msisdn && r.msisdn]
    .filter(Boolean).join(' · ') || '—'
}

function period(r) {
  if (r.date_from || r.date_to) return `${r.date_from || '…'} — ${r.date_to || '…'}`
  return r.period || '3d'
}

function tokens(r) {
  if (r.in_tokens == null && r.out_tokens == null) return '—'
  return `${(r.in_tokens || 0).toLocaleString('ru')} / ${(r.out_tokens || 0).toLocaleString('ru')}`
}

function Row({ r, showUser }) {
  const [open, setOpen] = useState(false)
  const span = showUser ? 7 : 6
  return (
    <>
      <tr className="hist-row" onClick={() => setOpen(!open)}>
        <td className="ts">{ts(r.at)}</td>
        {showUser && <td className="proj">{r.username || '—'}</td>}
        <td className="proj">{idents(r)}</td>
        <td>{(r.description || '').slice(0, 80)}{(r.description || '').length > 80 ? '…' : ''}</td>
        <td className="ts">{period(r)}</td>
        <td className="ts">{tokens(r)}</td>
        <td className="ts">{r.error ? <span className="error">ошибка</span> : '✓'}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={span} className="hist-detail">
            <div className="hint">Описание</div>
            <div className="msg">{r.description}</div>
            {r.error
              ? <><div className="hint">Ошибка</div><div className="msg error">{r.error}</div></>
              : <><div className="hint">Ответ · {r.llm_id}
                    {r.cost != null && ` · $${r.cost.toFixed(4)}`}
                    {r.duration_ms != null && ` · ${Math.round(r.duration_ms / 1000)}с`}</div>
                  <div className="msg">{r.response}</div></>}
          </td>
        </tr>
      )}
    </>
  )
}

// Reused by the admin users screen (per-user history log) — pass rows directly.
export function HistoryTable({ rows, showUser = false }) {
  if (!rows.length) return <div className="empty">Пока ни одного анализа.</div>
  return (
    <table>
      <thead>
        <tr><th>Время</th>{showUser && <th>Пользователь</th>}
            <th>Идентификаторы</th><th>Описание</th>
            <th>Период</th><th>Токены вх/исх</th><th></th></tr>
      </thead>
      <tbody>
        {rows.map((r) => <Row key={r.id} r={r} showUser={showUser} />)}
      </tbody>
    </table>
  )
}

export default function HistoryPanel() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getHistory().then(setRows).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!rows) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <HistoryTable rows={rows} showUser />
    </div>
  )
}
