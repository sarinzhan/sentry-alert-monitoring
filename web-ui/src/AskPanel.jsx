import { useEffect, useRef, useState } from 'react'
import { getMeta, getChats, getChat, deleteChat, chatMessageStream } from './api.js'
import Reasoning from './Reasoning.jsx'
import TokenPrompt from './TokenPrompt.jsx'

// Chat with the LLM — manager and admin only. Every conversation is one SDK
// session resumed on each follow-up, so the model remembers the whole
// exchange, its own earlier tool calls included. The answer is typed out
// live (stream deltas); «Стоп» aborts the fetch, which cancels the LLM run
// server-side.
export default function AskPanel({ onQuota }) {
  const [chats, setChats] = useState([])
  const [chatId, setChatId] = useState(null)      // null = new chat
  const [messages, setMessages] = useState([])
  const [question, setQuestion] = useState('')
  const [model, setModel] = useState('')
  const [meta, setMeta] = useState({ models: [], default_model: '' })
  const [busy, setBusy] = useState(false)
  const [steps, setSteps] = useState([])
  const [live, setLive] = useState('')            // text being typed out
  const [usage, setUsage] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [limitMsg, setLimitMsg] = useState(null)
  const [lastQuestion, setLastQuestion] = useState(null)
  const abortRef = useRef(null)
  const endRef = useRef(null)

  useEffect(() => { getMeta().then(setMeta).catch(() => {}) }, [])
  useEffect(() => { refreshChats() }, [])
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'nearest' }) },
            [messages, steps, live])

  function refreshChats() {
    getChats().then(setChats).catch(() => {})
  }

  function newChat() {
    if (busy) return
    setChatId(null)
    setMessages([])
    setSteps([])
    setLive('')
    setError('')
    setNotice('')
    setLimitMsg(null)
  }

  async function selectChat(id) {
    if (busy || id === chatId) return
    setError('')
    setNotice('')
    setLimitMsg(null)
    setSteps([])
    setLive('')
    try {
      const c = await getChat(id)
      setChatId(c.id)
      setMessages(c.messages)
    } catch (e) {
      setError(e.message)
    }
  }

  async function removeChat(e, id) {
    e.stopPropagation()
    if (busy || !window.confirm('Удалить этот чат?')) return
    try {
      await deleteChat(id)
      if (id === chatId) newChat()
      refreshChats()
    } catch (err) {
      setError(err.message)
    }
  }

  function stop() {
    abortRef.current?.abort()
  }

  async function run(q, isRetry = false) {
    if (busy || !q) return
    setLastQuestion(q)
    setBusy(true)
    setError('')
    setNotice('')
    setLimitMsg(null)
    setSteps([])
    setLive('')
    setUsage(null)
    if (!isRetry) setMessages((m) => [...m, { role: 'user', text: q }])
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let runSteps = []
    try {
      await chatMessageStream(
        { question: q, chat_id: chatId || undefined, model: model || undefined },
        (ev) => {
          if (ev.type === 'done') {
            setChatId(ev.chat_id)
            setMessages((m) => [...m, {
              role: 'assistant', text: ev.explanation, llm_id: ev.llm_id,
              in_tokens: ev.in_tokens, out_tokens: ev.out_tokens,
              cost: ev.cost, steps: runSteps,
            }])
            setSteps([])
            setLive('')
            refreshChats()
          } else if (ev.type === 'error') setError(ev.error)
          else if (ev.type === 'usage') setUsage(ev)
          else if (ev.type === 'delta') setLive((t) => t + ev.text)
          else { runSteps = [...runSteps, ev]; setSteps(runSteps); setLive('') }
        }, ctrl.signal)
      setQuestion('')
    } catch (e) {
      if (e.name === 'AbortError') {
        setNotice('⏹ Остановлено — ответ не сохранён.')
        setSteps([])
        setLive('')
        refreshChats()   // the user message is already stored server-side
      } else if (e.limitReached) setLimitMsg(e.message)
      else setError(e.message)
    } finally {
      abortRef.current = null
      setBusy(false)
      onQuota && onQuota()
    }
  }

  function submit(e) {
    e.preventDefault()
    run(question.trim())
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      run(question.trim())
    }
  }

  return (
    <div className="chat-layout">
      <aside className="chat-list">
        <button type="button" className="tab new-chat"
                onClick={newChat} disabled={busy}>+ Новый чат</button>
        {chats.map((c) => (
          <div key={c.id}
               className={c.id === chatId ? 'chat-item active' : 'chat-item'}
               onClick={() => selectChat(c.id)} title={c.title}>
            <span className="chat-title">{c.title || '(без названия)'}</span>
            <button type="button" className="chat-del" title="удалить"
                    onClick={(e) => removeChat(e, c.id)}>✕</button>
          </div>
        ))}
        {chats.length === 0 && <div className="hint">Чатов пока нет.</div>}
      </aside>

      <section className="chat-main">
        <div className="chat-messages">
          {messages.length === 0 && !busy && (
            <div className="empty">
              Модель помнит весь диалог — задавайте уточняющие вопросы.
              Инструменты Sentry, GitLab и заметки-память ей доступны.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`bubble ${m.role}`}>
              <div className="bubble-text">{m.text}</div>
              {m.role === 'assistant' && (
                <div className="hint">
                  анализ ИИ — проверьте выводы{m.llm_id && ` · ${m.llm_id}`}
                  {m.in_tokens != null &&
                    ` · токены: ${m.in_tokens.toLocaleString('ru')} вх / ${m.out_tokens.toLocaleString('ru')} исх`}
                  {m.cost != null && ` · $${m.cost.toFixed(4)}`}
                </div>
              )}
              {m.steps?.length > 0 && (
                <Reasoning steps={m.steps} running={false} usage={null} />
              )}
            </div>
          ))}
          {busy && <Reasoning steps={steps} running usage={usage} live={live} />}
          <div ref={endRef} />
        </div>

        {error && <p className="error">{error}</p>}
        {notice && <p className="hint">{notice}</p>}
        {limitMsg && (
          <TokenPrompt message={limitMsg}
                       onSaved={() => { setLimitMsg(null); run(lastQuestion, true) }} />
        )}

        <form className="chat-input" onSubmit={submit} noValidate>
          <textarea value={question} rows={2}
                    onChange={(e) => setQuestion(e.target.value)}
                    onKeyDown={onKey}
                    placeholder={chatId
                      ? 'Уточняющий вопрос… (Enter — отправить, Shift+Enter — новая строка)'
                      : 'Например: к каким проектам и инструментам у тебя есть доступ?'} />
          <div className="chat-controls">
            {meta.models.length > 0 && (
              <select value={model} onChange={(e) => setModel(e.target.value)}
                      title="Модель ИИ">
                <option value="">{`по умолчанию (${meta.default_model})`}</option>
                {meta.models.filter((m) => m !== meta.default_model).map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            )}
            {busy && (
              <button type="button" className="stop" onClick={stop}>⏹ Стоп</button>
            )}
            <button type="submit" disabled={busy || !question.trim()}>
              {busy ? 'Думаю…' : 'Отправить'}
            </button>
          </div>
        </form>
      </section>
    </div>
  )
}
