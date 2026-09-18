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
        <div className="hint token-help">
          Вставьте свой Claude токен — он сохранится в вашем аккаунте, и
          дальнейшие анализы пойдут через него. Получить токен можно двумя
          способами:
          <ol>
            <li>
              <b>API-ключ Anthropic</b> (sk-ant-api…): зарегистрируйтесь в{' '}
              <a href="https://console.anthropic.com/settings/keys"
                 target="_blank" rel="noreferrer">консоли Anthropic</a>{' '}
              → Settings → API Keys → «Create Key». Оплата по факту
              использования (нужна привязанная карта).
            </li>
            <li>
              <b>Токен подписки Claude Pro/Max</b> (sk-ant-oat…): установите{' '}
              <a href="https://docs.claude.com/en/docs/claude-code/overview"
                 target="_blank" rel="noreferrer">Claude Code</a>, войдите со
              своей подпиской{' '}
              <a href="https://claude.ai" target="_blank"
                 rel="noreferrer">claude.ai</a> и выполните в терминале{' '}
              <code>claude setup-token</code> — скопируйте выданный токен.
            </li>
          </ol>
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
