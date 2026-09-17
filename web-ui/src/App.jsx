import { useState } from 'react'
import InvestigateForm from './InvestigateForm.jsx'
import Results from './Results.jsx'
import ProjectsPanel from './ProjectsPanel.jsx'
import HistoryPanel from './HistoryPanel.jsx'
import Reasoning from './Reasoning.jsx'
import { investigate, explainStream } from './api.js'

export default function App() {
  const [view, setView] = useState('search')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [lastBody, setLastBody] = useState(null)
  const [explanation, setExplanation] = useState(null)
  const [explaining, setExplaining] = useState(false)
  const [explainError, setExplainError] = useState('')
  const [steps, setSteps] = useState([])
  const [usage, setUsage] = useState(null)

  async function onSubmit(body) {
    setBusy(true)
    setError('')
    setResult(null)
    setExplanation(null)
    setExplainError('')
    setSteps([])
    setUsage(null)
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
    setSteps([])
    setUsage(null)
    try {
      await explainStream(lastBody, (ev) => {
        if (ev.type === 'done') {
          setExplanation(ev)
          if (ev.in_tokens != null) setUsage({ in_tokens: ev.in_tokens, out_tokens: ev.out_tokens })
        } else if (ev.type === 'error') setExplainError(ev.error)
        else if (ev.type === 'usage') setUsage(ev)
        else setSteps((s) => [...s, ev])
      })
    } catch (e) {
      setExplainError(e.message)
    } finally {
      setExplaining(false)
    }
  }


  return (
    <main>
      <nav className="tabs">
        <button className={view === 'search' ? 'tab active' : 'tab'}
                onClick={() => setView('search')}>Расследование</button>
        <button className={view === 'projects' ? 'tab active' : 'tab'}
                onClick={() => setView('projects')}>Проекты</button>
        <button className={view === 'history' ? 'tab active' : 'tab'}
                onClick={() => setView('history')}>История</button>
      </nav>
      {view === 'projects' && (
        <>
          <h1>Проекты</h1>
          <ProjectsPanel />
        </>
      )}
      {view === 'history' && (
        <>
          <h1>История анализов</h1>
          <HistoryPanel />
        </>
      )}
      {view === 'search' && (
        <>
      <h1>Расследование проблемы</h1>
      <p className="sub">Поиск ошибок и логов в Sentry по запросу, устройству или абоненту.</p>
      <InvestigateForm onSubmit={onSubmit} busy={busy} serverError={error} />
      {result && (
        <>
          {!explanation && (
            <div className="explain-bar">
              <button onClick={onExplain} disabled={explaining}>
                {explaining ? 'Анализирую…' : '🤖 Объяснить простыми словами'}
              </button>
              <span className="error">{explainError}</span>
            </div>
          )}
          <Reasoning steps={steps} running={explaining} usage={usage} />
          {explanation && (
            <div className="explanation">
              <h2>Объяснение</h2>
              <div className="explanation-text">{explanation.explanation}</div>
              <div className="hint">
                анализ ИИ — проверьте выводы · {explanation.llm_id}
                {explanation.in_tokens != null &&
                  ` · токены: ${explanation.in_tokens.toLocaleString('ru')} вх / ${explanation.out_tokens.toLocaleString('ru')} исх`}
                {explanation.cost != null && ` · $${explanation.cost.toFixed(4)}`}
              </div>
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
