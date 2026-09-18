import { useEffect, useState } from 'react'
import { getSettings, saveSettings } from './api.js'

// Superadmin settings: daily LLM limits and the general system prompt.
// The prompt presets (roles / problem templates) live in PromptsPanel —
// editable by admin and superadmin.

function Limits({ settings, onSaved }) {
  // '' = unlimited (stored as -1); 0 = shared token forbidden; >0 = cap
  const show = (v) => (v < 0 ? '' : String(v))
  const [requests, setRequests] = useState(show(settings.llm_daily_requests))
  const [tokens, setTokens] = useState(show(settings.llm_daily_tokens))
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  async function save() {
    setBusy(true)
    setStatus('')
    try {
      onSaved(await saveSettings({
        llm_daily_requests: requests.trim() === '' ? null : Number(requests),
        llm_daily_tokens: tokens.trim() === '' ? null : Number(tokens),
      }))
      setStatus('✓ сохранено')
      setTimeout(() => setStatus(''), 2000)
    } catch (e) {
      setStatus(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="settings-block" onSubmit={(e) => { e.preventDefault(); save() }}>
      <div className="field">
        <label><b>Анализов в день</b> (на пользователя; 0 — запретить общий
          токен, пусто — без лимита)</label>
        <input type="number" min="0" value={requests} placeholder="без лимита"
               onChange={(e) => setRequests(e.target.value)} />
      </div>
      <div className="field">
        <label><b>Токенов в день</b> (вх+исх на пользователя; 0 — запретить,
          пусто — без лимита)</label>
        <input type="number" min="0" value={tokens} placeholder="без лимита"
               onChange={(e) => setTokens(e.target.value)} />
      </div>
      <div className="actions">
        <button type="submit" disabled={busy}>Сохранить лимиты</button>
        <span className="hint status">{status}</span>
      </div>
    </form>
  )
}

function SystemPrompt({ settings, onSaved }) {
  const [text, setText] = useState(settings.system_prompt)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  async function save() {
    setBusy(true)
    setStatus('')
    try {
      onSaved(await saveSettings({ system_prompt: text }))
      setStatus('✓ сохранено')
      setTimeout(() => setStatus(''), 2000)
    } catch (e) {
      setStatus(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="settings-block" onSubmit={(e) => { e.preventDefault(); save() }}>
      <div className="field wide">
        <label><b>Общий системный промпт</b> — описание вашей системы
          (сервисы, домены, термины); добавляется к каждому анализу</label>
        <textarea rows="8" value={text} onChange={(e) => setText(e.target.value)}
                  placeholder="Например: Мы — мобильный оператор. Основные сервисы: billing (оплаты и балансы), configurator (подключение пакетов)…" />
      </div>
      <div className="actions">
        <button type="submit" disabled={busy}>Сохранить промпт</button>
        <span className="hint status">{status}</span>
      </div>
    </form>
  )
}

export default function SettingsPanel() {
  const [settings, setSettings] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getSettings().then(setSettings).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!settings) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <h2>Дневные лимиты (общий Claude токен)</h2>
      <p className="sub">
        Действуют для каждого пользователя; после превышения анализы идут
        через личный токен пользователя. Меняются без перезапуска.
      </p>
      <Limits settings={settings} onSaved={setSettings} />
      <h2>Системный промпт</h2>
      <SystemPrompt settings={settings} onSaved={setSettings} />
    </div>
  )
}
