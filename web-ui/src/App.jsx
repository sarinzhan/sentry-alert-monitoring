import { useEffect, useState } from 'react'
import InvestigateForm from './InvestigateForm.jsx'
import Results from './Results.jsx'
import ProjectsPanel from './ProjectsPanel.jsx'
import HistoryPanel from './HistoryPanel.jsx'
import UsersPanel from './UsersPanel.jsx'
import Reasoning from './Reasoning.jsx'
import Login from './Login.jsx'
import TokenPrompt from './TokenPrompt.jsx'
import { investigate, explainStream, getMe, logout } from './api.js'

export default function App() {
  // undefined = checking the session, null = show login, object = logged in
  const [user, setUser] = useState(undefined)
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
  const [limitMsg, setLimitMsg] = useState(null)

  useEffect(() => {
    getMe().then(setUser).catch(() => setUser(null))
    const onExpired = () => setUser(null)
    window.addEventListener('auth-expired', onExpired)
    return () => window.removeEventListener('auth-expired', onExpired)
  }, [])

  async function onLogout() {
    try { await logout() } catch { /* session may already be gone */ }
    setUser(null)
    setView('search')
  }

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
    setLimitMsg(null)
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
      if (e.limitReached) setLimitMsg(e.message)
      else setExplainError(e.message)
    } finally {
      setExplaining(false)
      getMe().then((u) => u && setUser(u)).catch(() => {})  // refresh quota
    }
  }

  function onTokenSaved() {
    setLimitMsg(null)
    getMe().then((u) => u && setUser(u)).catch(() => {})
    onExplain()                                   // retry on the saved token
  }

  function quotaHint() {
    if (!user.llm_daily_limit) return null
    if (user.has_token) return 'личный токен подключён'
    const left = Math.max(0, user.llm_daily_limit - user.llm_used_today)
    return `осталось анализов сегодня: ${left} из ${user.llm_daily_limit}`
  }


  if (user === undefined) {
    return <main><div className="empty">Загружаю…</div></main>
  }
  if (!user) {
    return <main><Login onLogin={setUser} /></main>
  }

  const isAdmin = user.role === 'admin'

  return (
    <main>
      <div className="topbar">
        <nav className="tabs">
          <button className={view === 'search' ? 'tab active' : 'tab'}
                  onClick={() => setView('search')}>Расследование</button>
          <button className={view === 'history' ? 'tab active' : 'tab'}
                  onClick={() => setView('history')}>История</button>
          {isAdmin && (
            <button className={view === 'projects' ? 'tab active' : 'tab'}
                    onClick={() => setView('projects')}>Проекты</button>
          )}
          {isAdmin && (
            <button className={view === 'users' ? 'tab active' : 'tab'}
                    onClick={() => setView('users')}>Пользователи</button>
          )}
        </nav>
        <div className="userbox">
          <span>{user.username} · {user.role}</span>
          <button className="tab" onClick={onLogout}>Выйти</button>
        </div>
      </div>
      {view === 'projects' && isAdmin && (
        <>
          <h1>Проекты</h1>
          <ProjectsPanel />
        </>
      )}
      {view === 'users' && isAdmin && (
        <>
          <h1>Пользователи</h1>
          <UsersPanel />
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
              <span className="hint">{quotaHint()}</span>
              <span className="error">{explainError}</span>
            </div>
          )}
          {limitMsg && <TokenPrompt message={limitMsg} onSaved={onTokenSaved} />}
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
