import { useEffect, useState } from 'react'
import { getLlmHistory, getLlmCall } from './api.js'

// LLM calls made by the Sentry webhook pipeline — alert analysis, incident
// grouping, resolution checks — separate from the web investigation history.
// Each row expands into the full agentic trace: every tool the model called,
// with the argument values it passed and the result it got back.

function fmtTime(sec) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString('ru')
}

const KIND_LABEL = {
  alert: 'Алерт', group: 'Группировка', resolve: 'Проверка решения',
}

// readable, scrollable block for tool args/results and the prompt
const PRE = {
  whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: '20rem',
  overflow: 'auto', background: 'rgba(0,0,0,.04)', padding: '.5rem',
  borderRadius: '4px', fontSize: '.85em', margin: '.25rem 0',
}

function pretty(v) {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

function Step({ call, i }) {
  // one tool invocation: {tool, args, result}
  const [open, setOpen] = useState(false)
  return (
    <div className="llm-step">
      <div style={{ cursor: 'pointer' }} onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} <b>{i + 1}. {call.tool || 'tool'}</b>
      </div>
      {open && (
        <div style={{ paddingLeft: '1rem' }}>
          <div className="hint">аргументы</div>
          <pre style={PRE}>{pretty(call.args) || '—'}</pre>
          <div className="hint">результат</div>
          <pre style={PRE}>{pretty(call.result) || '—'}</pre>
        </div>
      )}
    </div>
  )
}

function Detail({ id }) {
  const [rec, setRec] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getLlmCall(id).then((d) => alive && setRec(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [id])

  if (error) return <div className="error">{error}</div>
  if (!rec) return <div className="hint">Загружаю…</div>

  const calls = rec.tool_calls || []
  return (
    <div className="incident-detail">
      <div className="field wide">
        <label><b>Шаги ({calls.length})</b> — какие инструменты вызвал LLM</label>
        {calls.length === 0 && <div className="hint">без инструментов (один запрос)</div>}
        {calls.map((c, i) => <Step key={i} call={c} i={i} />)}
      </div>
      {rec.tools_offered?.length > 0 && (
        <div className="hint">доступные инструменты: {rec.tools_offered.join(', ')}</div>
      )}
      <div className="field wide">
        <label><b>Ответ</b></label>
        <div className="explanation-text" style={{ whiteSpace: 'pre-wrap' }}>{rec.response || '—'}</div>
      </div>
      <details>
        <summary className="hint">промпт</summary>
        <pre className="llm-pre">{rec.prompt || '—'}</pre>
      </details>
    </div>
  )
}

const ymd = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`

export default function LlmHistoryPanel({ active }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(null)
  const [from, setFrom] = useState('')   // YYYY-MM-DD (local), '' = no bound
  const [to, setTo] = useState('')

  async function load() {
    // local day bounds -> epoch seconds
    const since = from ? new Date(`${from}T00:00:00`).getTime() / 1000 : undefined
    const until = to ? new Date(`${to}T23:59:59`).getTime() / 1000 : undefined
    try { setData(await getLlmHistory({ since, until })) }
    catch (e) { setError(e.message) }
  }

  useEffect(() => { if (active) load() }, [active, from, to])

  function lastDays(n) {
    const t = new Date(), f = new Date()
    f.setDate(f.getDate() - (n - 1))
    setFrom(ymd(f)); setTo(ymd(t))
  }
  function allTime() { setFrom(''); setTo('') }

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="empty">Загружаю…</div>

  const t = data.totals || {}

  return (
    <div>
      <p className="sub">
        Вызовы LLM из обработки вебхуков Sentry: разбор алертов, группировка
        инцидентов, проверка решения. Разверните запись, чтобы увидеть шаги —
        какие инструменты вызывал LLM, с какими аргументами и что получил.
      </p>

      <div className="actions" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
        <label className="hint">с <input type="date" value={from}
               onChange={(e) => setFrom(e.target.value)} /></label>
        <label className="hint">по <input type="date" value={to}
               onChange={(e) => setTo(e.target.value)} /></label>
        <button type="button" className="tab" onClick={() => lastDays(1)}>Сегодня</button>
        <button type="button" className="tab" onClick={() => lastDays(7)}>7 дней</button>
        <button type="button" className="tab" onClick={() => lastDays(30)}>30 дней</button>
        <button type="button" className="tab" onClick={allTime}>Всё</button>
        <button type="button" className="tab" onClick={load}>Обновить</button>
      </div>

      <div className="results" style={{ padding: '.5rem .75rem', margin: '.5rem 0' }}>
        <b>Запросов: {t.count || 0}</b>{' · '}
        вход: {(t.in_tokens || 0).toLocaleString('ru')}{' · '}
        выход: {(t.out_tokens || 0).toLocaleString('ru')}{' · '}
        всего: {((t.in_tokens || 0) + (t.out_tokens || 0)).toLocaleString('ru')} токенов
        {t.cost_usd ? ` · $${Number(t.cost_usd).toFixed(4)}` : ''}
      </div>

      {data.calls.length === 0 && <div className="empty">Пока нет вызовов.</div>}

      {data.calls.map((c) => (
        <div className="results tg-chat" key={c.id}>
          <h2 style={{ cursor: 'pointer' }}
              onClick={() => setOpen(open === c.id ? null : c.id)}>
            {open === c.id ? '▾' : '▸'}{' '}
            <span className="badge">{KIND_LABEL[c.kind] || c.kind}</span>{' '}
            <span className="hint">{fmtTime(c.at)}</span>
          </h2>
          <div className="hint">
            {c.issue_id && <>ошибка {c.issue_id} · </>}
            {c.model} · {c.turns} шаг(ов) · 🔧 {c.tool_count} ·
            {' '}{(c.in_tokens || 0).toLocaleString('ru')} вх / {(c.out_tokens || 0).toLocaleString('ru')} исх
            {c.cost_usd != null && <> · ${Number(c.cost_usd).toFixed(4)}</>}
            {c.duration_ms != null && <> · {(c.duration_ms / 1000).toFixed(1)}с</>}
            {' '}· <code>{c.id}</code>
          </div>
          {c.preview && open !== c.id && (
            <div className="hint" style={{ marginTop: '.25rem' }}>{c.preview}</div>
          )}
          {open === c.id && <Detail id={c.id} />}
        </div>
      ))}
    </div>
  )
}
