import { useEffect, useState } from 'react'
import { getLlmGaps, dismissLlmGap } from './api.js'

// «Что нужно LLM» — what the model flagged as missing while investigating
// (no logs in Sentry, too few logs, no source access, …). The model reports
// these via the report_gap tool during alert analysis / incident verification /
// web investigation; here a human sees what to add.

function fmtTime(sec) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString('ru')
}

export default function GapsPanel({ active }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    try { setData(await getLlmGaps()) }
    catch (e) { setError(e.message) }
  }

  useEffect(() => { if (active) load() }, [active])

  async function dismiss(id) {
    setBusy(true)
    try { await dismissLlmGap(id); await load() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="empty">Загружаю…</div>

  const label = (k) => data.kinds?.[k] || k

  return (
    <div>
      <p className="sub">
        Что LLM не хватило для анализа — модель отмечает это сама во время разбора
        алертов, проверки инцидентов и расследований. Используйте, чтобы понять,
        где добавить логирование, подключить репозиторий или дать доступ.
      </p>

      <div className="actions">
        <button type="button" className="tab" onClick={load} disabled={busy}>Обновить</button>
      </div>

      {data.gaps.length === 0 && (
        <div className="empty">Пока ничего — LLM всего хватает 🎉</div>
      )}

      {data.gaps.map((g) => (
        <div className="results tg-chat" key={g.id}>
          <h2>
            <span className="badge" style={{ background: '#b45309' }}>{label(g.kind)}</span>{' '}
            {g.project || 'проект неизвестен'}
            {g.count > 1 && <span className="hint"> · ×{g.count}</span>}
          </h2>
          {g.detail && <div className="explanation-text">{g.detail}</div>}
          <div className="hint">
            впервые: {fmtTime(g.first_seen)} · последний раз: {fmtTime(g.last_seen)}
            {g.issue_id && <> · ошибка {g.issue_id}</>}
          </div>
          <div className="actions">
            {g.source_url
              ? <a className="tab" href={g.source_url} target="_blank" rel="noreferrer">Исходники →</a>
              : (g.repo ? <span className="hint">репозиторий: {g.repo}</span>
                        : <span className="hint">репозиторий проекта не привязан</span>)}
            <button type="button" className="tab" disabled={busy}
                    onClick={() => dismiss(g.id)}>Скрыть</button>
          </div>
        </div>
      ))}
    </div>
  )
}
