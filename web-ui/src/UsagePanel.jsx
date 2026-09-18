import { useEffect, useState } from 'react'
import { getUsage, getUsers } from './api.js'

// LLM usage stats («Статистика» tab): requests and tokens per local day,
// split between the shared (system) token and the user's personal one.
// Everyone sees their own numbers; the admin can pick any user.
const PERIODS = [['today', 'Сегодня'], ['7d', 'Неделя'], ['30d', 'Месяц']]

const fmt = (n) => (n || 0).toLocaleString('ru')

function Cell({ b }) {
  if (!b.requests) return <td className="dim">—</td>
  return (
    <td>
      {b.requests} · {fmt(b.in_tokens)} вх / {fmt(b.out_tokens)} исх
      {b.cost > 0 && ` · $${b.cost.toFixed(4)}`}
    </td>
  )
}

export default function UsagePanel({ user, active }) {
  const isAdmin = user.role === 'admin'
  const [period, setPeriod] = useState('7d')
  const [target, setTarget] = useState('')       // '' = own stats
  const [users, setUsers] = useState([])
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (isAdmin && active) getUsers().then(setUsers).catch(() => {})
  }, [isAdmin, active])

  useEffect(() => {
    if (!active) return
    setError('')
    getUsage(period, target || undefined)
      .then(setData)
      .catch((e) => setError(e.message))
  }, [active, period, target])

  return (
    <>
      <div className="usage-controls">
        <div className="tabs">
          {PERIODS.map(([p, label]) => (
            <button key={p} type="button"
                    className={period === p ? 'tab active' : 'tab'}
                    onClick={() => setPeriod(p)}>{label}</button>
          ))}
        </div>
        {isAdmin && users.length > 0 && (
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{`я (${user.username})`}</option>
            {users.filter((u) => u.username !== user.username).map((u) => (
              <option key={u.username} value={u.username}>{u.username}</option>
            ))}
          </select>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      {data && (
        <>
          <p className="sub">
            Пользователь: <b>{data.username}</b> · анализы · токены вх / исх ·
            стоимость (только при api-key). Дни местные (UTC+6).
          </p>
          {data.days.length === 0 ? (
            <div className="empty">За этот период запусков не было.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Дата</th>
                  <th>Общий токен</th>
                  <th>Личный токен</th>
                </tr>
              </thead>
              <tbody>
                {data.days.map((d) => (
                  <tr key={d.day}>
                    <td className="ts">{d.day}</td>
                    <Cell b={d.shared} />
                    <Cell b={d.own} />
                  </tr>
                ))}
                <tr className="totals">
                  <td><b>Итого</b></td>
                  <Cell b={data.totals.shared} />
                  <Cell b={data.totals.own} />
                </tr>
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  )
}
