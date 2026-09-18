import { useEffect, useState } from 'react'
import { getPrompts, createPrompt, savePrompt, deletePrompt } from './api.js'

// Prompt presets editor — available to manager and admin.
// kind='role': who the analysis answer is written for; kind='problem':
// templates that prefill the description field in the form.

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

export function Presets({ kind, title, hint, items, setItems }) {
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

export default function PromptsPanel() {
  const [roles, setRoles] = useState([])
  const [problems, setProblems] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    getPrompts().then((p) => { setRoles(p.roles); setProblems(p.problems); setLoaded(true) })
      .catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!loaded) return <div className="empty">Загружаю…</div>

  return (
    <div className="results">
      <Presets kind="role" title="Роли ответа" items={roles} setItems={setRoles}
               hint="Для кого пишется ответ анализа (выбирается в форме расследования): клиент, тестировщик, поддержка, разработчик. Текст заменяет раздел о стиле ответа в промпте." />
      <Presets kind="problem" title="Шаблоны проблем" items={problems}
               setItems={setProblems}
               hint="Готовые описания типовых проблем — подставляются в поле «Описание проблемы» в форме расследования." />
    </div>
  )
}
