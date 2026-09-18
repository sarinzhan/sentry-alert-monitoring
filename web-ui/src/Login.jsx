import { useState } from 'react'
import { login } from './api.js'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('Введите логин и пароль')
      return
    }
    setBusy(true)
    setError('')
    try {
      onLogin(await login(username.trim(), password))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <h1>Sentry Monitor</h1>
      <p className="sub">Войдите, чтобы продолжить.</p>
      <form onSubmit={submit}>
        <div className="field">
          <label><b>Логин</b></label>
          <input value={username} autoFocus autoComplete="username"
                 onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label><b>Пароль</b></label>
          <input type="password" value={password} autoComplete="current-password"
                 onChange={(e) => setPassword(e.target.value)} />
        </div>
        <div className="actions">
          <button type="submit" disabled={busy}>{busy ? '…' : 'Войти'}</button>
          <span className="error">{error}</span>
        </div>
      </form>
    </div>
  )
}
