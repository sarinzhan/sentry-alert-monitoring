import { useState } from 'react'
import InvestigateForm from './InvestigateForm.jsx'
import Results from './Results.jsx'
import ProjectsPanel from './ProjectsPanel.jsx'
import { investigate, explain } from './api.js'

export default function App() {
  const [view, setView] = useState('search')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [lastBody, setLastBody] = useState(null)
  const [explanation, setExplanation] = useState(null)
  const [explaining, setExplaining] = useState(false)
  const [explainError, setExplainError] = useState('')

  async function onSubmit(body) {
    setBusy(true)
    setError('')
    setResult(null)
    setExplanation(null)
    setExplainError('')
    setLastBody(body)
    try {
      setResult(await investigate(body))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function onExplain() {
    if (!lastBody || explaining) return
    setExplaining(true)
    setExplainError('')
    try {
      setExplanation(await explain(lastBody))
    } catch (e) {
      setExplainError(e.message)
    } finally {
      setExplaining(false)
    }
  }

  const found = result && (result.count > 0 || result.logs?.length > 0)

  return (
    <main>
      <nav className="tabs">
        <button className={view === 'search' ? 'tab active' : 'tab'}
                onClick={() => setView('search')}>Расследование</button>
        <button className={view === 'projects' ? 'tab active' : 'tab'}
                onClick={() => setView('projects')}>Проекты</button>
      </nav>
      {view === 'projects' && (
        <>
          <h1>Проекты</h1>
          <ProjectsPanel />
        </>
      )}
      {view === 'search' && (
        <>
      <h1>Расследование проблемы</h1>
      <p className="sub">Поиск ошибок и логов в Sentry по запросу, устройству или абоненту.</p>
      <InvestigateForm onSubmit={onSubmit} busy={busy} serverError={error} />
      {result && (
        <>
          {found && !explanation && (
            <div className="explain-bar">
              <button onClick={onExplain} disabled={explaining}>
                {explaining ? 'Анализирую… это может занять пару минут' : '🤖 Объяснить простыми словами'}
              </button>
              <span className="error">{explainError}</span>
            </div>
          )}
          {explanation && (
            <div className="explanation">
              <h2>Объяснение</h2>
              <div className="explanation-text">{explanation.explanation}</div>
              <div className="hint">анализ ИИ — проверьте выводы · {explanation.llm_id}</div>
            </div>
          )}
          <Results data={result} />
        </>
      )}
        </>
      )}
    </main>
  )
}
