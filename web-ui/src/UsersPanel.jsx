import { useEffect, useState } from 'react'
import { getUsers, createUser, saveUser, deleteUser, getUserHistory } from './api.js'
import { HistoryTable } from './HistoryPanel.jsx'

const ts = (at) => (at ? new Date(at * 1000).toLocaleString('ru-RU') : '—')

function UserHistory({ userId }) {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getUserHistory(userId).then(setRows).catch((e) => setError(e.message))
  }, [userId])

  if (error) return <div className="error">{error}</div>
  if (!rows) return <div className="empty">Загружаю…</div>
  return <HistoryTable rows={rows} />
}

function Row({ user, onSaved, onDeleted }) {
  const [username, setUsername] = useState(user.username)
  const [password, setPassword] = useState(user.password)
  const [role, setRole] = useState(user.role)
  const [showPass, setShowPass] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('')

  const dirty = username !== user.username || password !== user.password
    || role !== user.role

  async function save() {
    setSaving(true)
    setStatus('')
    try {
      const row = await saveUser(user.id, { username, password, role })
      onSaved(row)
      setStatus('✓ сохранено')
      setTimeout(() => setStatus(''), 2000)
    } catch (e) {
      setStatus(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!window.confirm(`Удалить пользователя «${user.username}»?`)) return
    setSaving(true)
    setStatus('')
    try {
      await deleteUser(user.id)
      onDeleted(user.id)
    } catch (e) {
      setStatus(e.message)
      setSaving(false)
    }
  }

  return (
    <>
      <tr>
        <td>
          <input value={username} onChange={(e) => setUsername(e.target.value)} />
        </td>
        <td>
          <div className="pass-cell">
            <input type={showPass ? 'text' : 'password'} value={password}
                   onChange={(e) => setPassword(e.target.value)} />
            <button type="button" className="tab" title="показать/скрыть"
                    onClick={() => setShowPass(!showPass)}>
              {showPass ? '🙈' : '👁'}
            </button>
          </div>
        </td>
        <td>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="admin">admin</option>
            <option value="manager">manager</option>
            <option value="user">user</option>
          </select>
        </td>
        <td className="ts">{ts(user.last_activity)}</td>
        <td className="ts">{user.has_token ? 'есть' : '—'}</td>
        <td className="ts">
          <button onClick={save} disabled={!dirty || saving}>
            {saving ? '…' : 'Сохранить'}
          </button>{' '}
          <button type="button" className="tab"
                  onClick={() => setShowHistory(!showHistory)}>
            {showHistory ? 'Скрыть историю' : 'История'}
          </button>{' '}
          <button type="button" className="tab danger" onClick={remove}
                  disabled={saving}>Удалить</button>
          <div className="hint status">{status}</div>
        </td>
      </tr>
      {showHistory && (
        <tr>
          <td colSpan="6" className="hist-detail">
            <div className="hint">История анализов — {user.username}</div>
            <UserHistory userId={user.id} />
          </td>
        </tr>
      )}
    </>
  )
}

function AddUser({ onCreated }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('user')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function add(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onCreated(await createUser({ username: username.trim(), password, role }))
      setUsername('')
      setPassword('')
      setRole('user')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="add-user" onSubmit={add}>
      <div className="field">
        <label><b>Логин</b></label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
      </div>
      <div className="field">
        <label><b>Пароль</b></label>
        <input value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      <div className="field">
        <label><b>Роль</b></label>
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="user">user</option>
          <option value="manager">manager</option>
          <option value="admin">admin</option>
        </select>
      </div>
      <div className="actions">
        <button type="submit" disabled={busy || !username.trim() || !password}>
          {busy ? '…' : 'Добавить пользователя'}
        </button>
        <span className="error">{error}</span>
      </div>
    </form>
  )
}

export default function UsersPanel() {
  const [users, setUsers] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getUsers().then(setUsers).catch((e) => setError(e.message))
  }, [])

  const onSaved = (row) =>
    setUsers((list) => list.map((u) => (u.id === row.id ? row : u)))
  const onDeleted = (id) =>
    setUsers((list) => list.filter((u) => u.id !== id))
  const onCreated = (row) => setUsers((list) => [...list, row])

  if (error) return <div className="error">{error}</div>
  if (!users) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <p className="sub">
        Доступ в веб-интерфейс: <b>admin</b> — всё (пользователи,
        проекты, настройки, промпты); <b>manager</b> — расследование, история
        и редактирование промптов (роли ответа и шаблоны проблем);
        <b> user</b> — расследование и история. Логин и пароль можно менять
        прямо в таблице.
      </p>
      <table>
        <thead>
          <tr><th>Логин</th><th>Пароль</th><th>Роль</th>
              <th>Последняя активность</th><th>Свой токен</th><th></th></tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <Row key={u.id} user={u} onSaved={onSaved} onDeleted={onDeleted} />
          ))}
        </tbody>
      </table>
      <h2>Новый пользователь</h2>
      <AddUser onCreated={onCreated} />
    </div>
  )
}
