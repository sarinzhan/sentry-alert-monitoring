import { useEffect, useState } from 'react'
import { getSettings, saveSettings, getPrompts, createPrompt, savePrompt,
         deletePrompt } from './api.js'

// Admin settings: daily LLM limits, the general system prompt, and the
// prepared prompts (role presets + problem templates) shown in the form.

function Limits({ settings, onSaved }) {
  const [requests, setRequests] = useState(String(settings.llm_daily_requests))
  const [tokens, setTokens] = useState(String(settings.llm_daily_tokens))
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  async function save() {
    setBusy(true)
    setStatus('')
    try {
      onSaved(await saveSettings({
        llm_daily_requests: Number(requests) || 0,
        llm_daily_tokens: Number(tokens) || 0,
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
        <label><b>Анализов в день</b> (на пользователя, 0 — без лимита)</label>
        <input type="number" min="0" value={requests}
               onChange={(e) => setRequests(e.target.value)} />
      </div>
      <div className="field">
        <label><b>Токенов в день</b> (вх+исх на пользователя, 0 — без лимита)</label>
        <input type="number" min="0" value={tokens}
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

function PresetRow({ preset, onSaved, onDeleted }) {
  const [name, setName] = useState(preset.name)
  const [text, setText] = useState(preset.text)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const dirty = name !== preset.name || text !== preset.text

  async function save() {
    setBusy(true)
    setStatus('')
    try {
      onSaved(await savePrompt(preset.id, { name, text }))
      setStatus('✓')
      setTimeout(() => setStatus(''), 2000)
    } catch (e) {
      setStatus(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!window.confirm(`Удалить «${preset.name}»?`)) return
    setBusy(true)
    try {
      await deletePrompt(preset.id)
      onDeleted(preset.id)
    } catch (e) {
      setStatus(e.message)
      setBusy(false)
    }
  }

  return (
    <div className="preset">
      <div className="preset-head">
        <input value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={save} disabled={!dirty || busy}>Сохранить</button>
        <button className="tab danger" onClick={remove} disabled={busy}>Удалить</button>
        <span className="hint status">{status}</span>
      </div>
      <textarea rows="5" value={text} onChange={(e) => setText(e.target.value)} />
    </div>
  )
}

function Presets({ kind, title, hint, items, setItems }) {
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const onSaved = (row) =>
    setItems((list) => list.map((p) => (p.id === row.id ? row : p)))
  const onDeleted = (id) => setItems((list) => list.filter((p) => p.id !== id))

  async function add(e) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const row = await createPrompt({ kind, name, text })
      setItems((list) => [...list, row])
      setName('')
      setText('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="settings-block">
      <h2>{title}</h2>
      <p className="sub">{hint}</p>
      {items.map((p) => (
        <PresetRow key={p.id} preset={p} onSaved={onSaved} onDeleted={onDeleted} />
      ))}
      <form className="preset add" onSubmit={add}>
        <div className="preset-head">
          <input value={name} placeholder="название"
                 onChange={(e) => setName(e.target.value)} />
          <button type="submit" disabled={busy || !name.trim() || !text.trim()}>
            Добавить
          </button>
          <span className="error">{error}</span>
        </div>
        <textarea rows="4" value={text} placeholder="текст промпта"
                  onChange={(e) => setText(e.target.value)} />
      </form>
    </div>
  )
}

export default function SettingsPanel() {
  const [settings, setSettings] = useState(null)
  const [roles, setRoles] = useState([])
  const [problems, setProblems] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    getSettings().then(setSettings).catch((e) => setError(e.message))
    getPrompts().then((p) => { setRoles(p.roles); setProblems(p.problems) })
      .catch((e) => setError(e.message))
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
      <Presets kind="role" title="Роли ответа" items={roles} setItems={setRoles}
               hint="Для кого пишется ответ анализа (выбирается в форме расследования): клиент, тестировщик, поддержка, разработчик. Текст заменяет раздел о стиле ответа в промпте." />
      <Presets kind="problem" title="Шаблоны проблем" items={problems}
               setItems={setProblems}
               hint="Готовые описания типовых проблем — подставляются в поле «Описание проблемы» в форме расследования." />
    </div>
  )
}
