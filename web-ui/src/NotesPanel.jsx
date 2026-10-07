import { useEffect, useMemo, useState } from 'react'
import { getNotes, deleteNote } from './api.js'

// Read view of the LLM's notes memory (the `knowledge` table). These are
// navigation hints the model saves at the end of an investigation and searches
// at the start of the next — where to look in this system, not incident facts.
// Mirrors the /notes Telegram command; here you can browse and prune them.

function fmtTime(sec) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString('ru')
}

export default function NotesPanel({ active }) {
  const [notes, setNotes] = useState(null)
  const [error, setError] = useState('')
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    try { setNotes(await getNotes()) }
    catch (e) { setError(e.message) }
  }

  useEffect(() => { if (active) load() }, [active])

  async function remove(id) {
    setBusy(true)
    try { await deleteNote(id); await load() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const shown = useMemo(() => {
    if (!notes) return []
    const t = q.trim().toLowerCase()
    if (!t) return notes
    return notes.filter((n) =>
      `${n.topic} ${n.content}`.toLowerCase().includes(t))
  }, [notes, q])

  if (error) return <div className="error">{error}</div>
  if (!notes) return <div className="empty">Загружаю…</div>

  return (
    <div>
      <p className="sub">
        Память LLM: подсказки «где что искать» в этой системе, которые модель
        сохраняет между расследованиями. Здесь можно просмотреть и удалить
        устаревшие. То же, что команда <code>/notes</code>.
      </p>

      <div className="actions">
        <input placeholder="Поиск по заметкам…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ minWidth: '16rem' }} />
        <button type="button" className="tab" onClick={load} disabled={busy}>Обновить</button>
        <span className="hint">{shown.length} из {notes.length}</span>
      </div>

      {notes.length === 0 && <div className="empty">Заметок пока нет.</div>}

      {shown.map((n) => (
        <div className="results tg-chat" key={n.id}>
          <h2>
            {n.topic}
            {n.source && <span className="hint"> · {n.source}</span>}
            {!n.embedded && <span className="hint"> · без вектора</span>}
          </h2>
          <div className="explanation-text" style={{ whiteSpace: 'pre-wrap' }}>{n.content}</div>
          <div className="actions">
            <span className="hint">обновлено: {fmtTime(n.updated)}</span>
            <button type="button" className="tab" disabled={busy}
                    onClick={() => remove(n.id)}>Удалить</button>
          </div>
        </div>
      ))}
    </div>
  )
}
