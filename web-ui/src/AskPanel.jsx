import { useEffect, useState } from 'react'
import { askStream, getMeta } from './api.js'
import Reasoning from './Reasoning.jsx'
import TokenPrompt from './TokenPrompt.jsx'

// Free-form question to the LLM — manager and admin only. Only the admin
// system prompt + the question are sent: no investigation template, no role
// presets. The model keeps its Sentry/GitLab tools, so it can look things up
// («к каким проектам у тебя есть доступ?»).
export default function AskPanel({ onQuota }) {
  const [question, setQuestion] = useState('')
  const [model, setModel] = useState('')
  const [meta, setMeta] = useState({ models: [], default_model: '' })
  const [busy, setBusy] = useState(false)
  const [steps, setSteps] = useState([])
  const [usage, setUsage] = useState(null)
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState('')
  const [limitMsg, setLimitMsg] = useState(null)
  const [lastQuestion, setLastQuestion] = useState(null)

  useEffect(() => { getMeta().then(setMeta).catch(() => {}) }, [])

  async function run(q) {
    if (busy || !q) return
    setLastQuestion(q)
    setBusy(true)
    setError('')
    setLimitMsg(null)
    setSteps([])
    setUsage(null)
    setAnswer(null)
    try {
      await askStream({ question: q, model: model || undefined }, (ev) => {
        if (ev.type === 'done') {
          setAnswer(ev)
          if (ev.in_tokens != null) setUsage({ in_tokens: ev.in_tokens, out_tokens: ev.out_tokens })
        } else if (ev.type === 'error') setError(ev.error)
        else if (ev.type === 'usage') setUsage(ev)
        else setSteps((s) => [...s, ev])
      })
    } catch (e) {
      if (e.limitReached) setLimitMsg(e.message)
      else setError(e.message)
    } finally {
      setBusy(false)
      onQuota && onQuota()
    }
  }

  function submit(e) {
    e.preventDefault()
    run(question.trim())
  }

  return (
    <>
      <form onSubmit={submit} noValidate>
        <div className="field wide">
          <label htmlFor="question"><b>Вопрос</b> <span className="req">*</span></label>
          <textarea id="question" value={question}
                    onChange={(e) => setQuestion(e.target.value)}
                    placeholder="Например: к каким проектам и инструментам у тебя есть доступ?" />
          <span className="hint">
            Свободный вопрос: модель получает только общий системный промпт и
            ваш текст — без шаблонов расследования и ролей ответа. Инструменты
            Sentry и GitLab ей доступны.
          </span>
        </div>
        {meta.models.length > 0 && (
          <div className="field">
            <label htmlFor="ask-model">Модель ИИ</label>
            <select id="ask-model" value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">{`по умолчанию (${meta.default_model})`}</option>
              {meta.models.filter((m) => m !== meta.default_model).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
        )}
        <div className="actions">
          <button type="submit" disabled={busy || !question.trim()}>
            {busy ? 'Спрашиваю…' : '🤖 Спросить'}
          </button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {limitMsg && (
        <TokenPrompt message={limitMsg}
                     onSaved={() => { setLimitMsg(null); run(lastQuestion) }} />
      )}
      <Reasoning steps={steps} running={busy} usage={usage} />
      {answer && (
        <div className="explanation">
          <h2>Ответ</h2>
          <div className="explanation-text">{answer.explanation}</div>
          <div className="hint">
            анализ ИИ — проверьте выводы · {answer.llm_id}
            {answer.in_tokens != null &&
              ` · токены: ${answer.in_tokens.toLocaleString('ru')} вх / ${answer.out_tokens.toLocaleString('ru')} исх`}
            {answer.cost != null && ` · $${answer.cost.toFixed(4)}`}
          </div>
        </div>
      )}
    </>
  )
}
