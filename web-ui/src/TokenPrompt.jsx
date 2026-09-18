import { useState } from 'react'
import { setToken } from './api.js'

// Shown when the daily shared-token quota is spent: the user saves a personal
// Claude token (OAuth from `claude setup-token` or an Anthropic API key) and
// their analyses continue on it, without the limit.
export default function TokenPrompt({ message, onSaved, allowClear = false }) {
  const [token, setTokenValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function save(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await setToken(token.trim())
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function clear() {
    setBusy(true)
    setError('')
    try {
      await setToken('')
      onSaved()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="token-prompt" onSubmit={save}>
      <div className="field wide">
        <label><b>{message || 'Дневной лимит анализов исчерпан'}</b></label>
        <div className="hint">
          Вставьте свой Claude токен: OAuth-токен подписки из{' '}
          <code>claude setup-token</code> (sk-ant-oat…) или API-ключ Anthropic
          (sk-ant-api…). Токен сохранится в вашем аккаунте, дальнейшие анализы
          пойдут через него.
        </div>
        <input value={token} placeholder="sk-ant-…" autoFocus
               onChange={(e) => setTokenValue(e.target.value)} />
      </div>
      <div className="actions">
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? '…' : 'Сохранить токен'}
        </button>
        {allowClear && (
          <button type="button" className="tab danger" onClick={clear}
                  disabled={busy}>Удалить сохранённый токен</button>
        )}
        <span className="error">{error}</span>
      </div>
    </form>
  )
}
