import { PERIODS } from './InvestigateForm.jsx'

function ts(value) {
  return (value || '').replace('T', ' ').slice(0, 19)
}

const PERIOD_LABELS = Object.fromEntries(PERIODS)

function Summary({ data }) {
  const w = data.window || {}
  const period = w.stats_period
    ? `за ${PERIOD_LABELS[w.stats_period] || w.stats_period}`
    : `${ts(w.start)} — ${ts(w.end)} UTC`
  return (
    <div className="summary">
      Искал {period}, {data.environment ? <>среда <code>{data.environment}</code></> : 'все среды'}:{' '}
      {(data.searches || []).map((s, i) => (
        <span key={s.id_type}>
          {i > 0 && '; '}
          {s.id_type}=<code>{s.value}</code>
          {s.matched_field ? <> (поле <code>{s.matched_field}</code>, {s.count})</> : ' (0)'}
        </span>
      ))}
    </div>
  )
}

function EventsTable({ events }) {
  return (
    <table>
      <thead>
        <tr><th>Время (UTC)</th><th>Сервис</th><th>Ошибка</th><th>Среда</th></tr>
      </thead>
      <tbody>
        {events.map((ev) => (
          <tr key={ev.id}>
            <td className="ts">{ts(ev.timestamp)}</td>
            <td className="proj">{ev.project || '?'}</td>
            <td>{ev.title || ev.message || '?'}</td>
            <td className="proj">{ev.environment || ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Logs({ logs }) {
  return logs.map((row, i) => (
    <div className="log" key={i}>
      <span className="meta">{ts(row.timestamp)} · {row['resource.service.name'] || ''}</span>
      <span className="msg">{row.message || ''}</span>
    </div>
  ))
}

export default function Results({ data }) {
  return (
    <div className="results">
      <Summary data={data} />
      <h2>Ошибки ({data.count})</h2>
      {data.count
        ? <EventsTable events={data.events} />
        : <div className="empty">Ошибок не найдено. Попробуйте другое время или другой идентификатор.</div>}
      {data.logs?.length > 0 && (
        <>
          <h2>Логи ({data.logs.length})</h2>
          <Logs logs={data.logs} />
        </>
      )}
    </div>
  )
}
