import { useEffect, useState } from 'react'
import {
  getTelegramChats, tgSubscribe, tgUnsubscribe, tgSaveRules, tgResetRules,
} from './api.js'

// Web mirror of the bot's /subscribe, /alerts and /set commands (admin only):
// one card per Telegram chat the bot knows, editing its project subscriptions,
// alert statuses and trigger-rule overrides.

const RULE_FIELDS = [
  ['ongoing_sec', 'Интервал напоминаний', 'dur'],
  ['critical_window_sec', 'Окно «критично»', 'dur'],
  ['critical_threshold', 'Порог событий (критично)', 'int'],
  ['affected_user_threshold', 'Порог затронутых абонентов', 'int'],
  ['critical_ratelimit_sec', 'Антиспам критичных', 'dur'],
  ['project_window_sec', 'Окно на проект (0 — выкл)', 'dur'],
  ['stat_windows', 'Окна статистики', 'win'],
]

function fmtDur(sec) {
  sec = Number(sec)
  if (!Number.isFinite(sec)) return ''
  for (const [u, n] of [['d', 86400], ['h', 3600], ['m', 60]])
    if (sec && sec % n === 0) return `${sec / n}${u}`
  return `${sec}s`
}

function showVal(v, kind) {
  if (v == null) return ''
  if (kind === 'win') return (Array.isArray(v) ? v : []).map(fmtDur).join('/')
  if (kind === 'dur') return fmtDur(v)
  return String(v)
}

function chatLabel(chat) {
  if (chat.title) return chat.title
  if (chat.username) return `@${chat.username}`
  return `Чат ${chat.chat_id}`
}

function ChatCard({ chat, projects, allStatuses, defaults, onChanged }) {
  // rule inputs (as strings), re-seeded whenever the chat data changes
  const initRules = () =>
    Object.fromEntries(RULE_FIELDS.map(([k, , kind]) => [k, showVal(chat.rules[k], kind)]))
  const initStatuses = () =>
    new Set(chat.statuses === null ? allStatuses : chat.statuses)

  const [edits, setEdits] = useState(initRules)
  const [statuses, setStatuses] = useState(initStatuses)
  const [newProject, setNewProject] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  useEffect(() => { setEdits(initRules()); setStatuses(initStatuses()) }, [chat])

  const overridden = new Set(chat.overrides || [])

  async function run(fn) {
    setBusy(true)
    setStatus('')
    try { await fn(); await onChanged() }
    catch (e) { setStatus(e.message) }
    finally { setBusy(false) }
  }

  function toggleStatus(s) {
    setStatuses((prev) => {
      const next = new Set(prev)
      next.has(s) ? next.delete(s) : next.add(s)
      return next
    })
  }

  async function saveRules() {
    // send only fields the admin actually changed
    const base = initRules()
    const rules = {}
    for (const [k] of RULE_FIELDS) {
      if (edits[k] !== base[k]) rules[k] = edits[k].trim() === '' ? null : edits[k].trim()
    }
    const allOn = allStatuses.every((s) => statuses.has(s))
    const nextStatuses = allOn ? null : allStatuses.filter((s) => statuses.has(s))
    const curSet = new Set(chat.statuses === null ? allStatuses : chat.statuses)
    const statusesChanged = curSet.size !== statuses.size ||
      [...statuses].some((s) => !curSet.has(s))
    const body = {}
    if (Object.keys(rules).length) body.rules = rules
    if (statusesChanged) body.statuses = nextStatuses
    if (!body.rules && !('statuses' in body)) { setStatus('нет изменений'); return }
    await run(async () => {
      await tgSaveRules(chat.chat_id, body)
      setStatus('✓ сохранено')
      setTimeout(() => setStatus(''), 2000)
    })
  }

  return (
    <div className="results tg-chat">
      <h2>
        {chatLabel(chat)}{' '}
        <span className="hint">{chat.type || ''} · id {chat.chat_id}</span>
      </h2>

      {/* subscriptions */}
      <div className="field wide">
        <label><b>Подписки на проекты</b></label>
        <div className="chips">
          {chat.subscriptions.length === 0 && <span className="hint">нет подписок</span>}
          {chat.subscriptions.map((s) => (
            <span className="chip" key={s.project}>
              {s.project === '*' ? 'все проекты' : s.project}
              {s.thread_id ? ` (тема ${s.thread_id})` : ''}
              <button type="button" disabled={busy} title="Отписать"
                      onClick={() => run(() => tgUnsubscribe(chat.chat_id, s.project))}>×</button>
            </span>
          ))}
        </div>
        <div className="actions">
          <select value={newProject} onChange={(e) => setNewProject(e.target.value)}>
            <option value="">— добавить проект —</option>
            <option value="*">все проекты</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id}{p.name ? ` · ${p.name}` : ''}
              </option>
            ))}
          </select>
          <button type="button" disabled={busy || !newProject}
                  onClick={() => run(async () => {
                    await tgSubscribe(chat.chat_id, newProject); setNewProject('')
                  })}>Подписать</button>
        </div>
      </div>

      {/* alert statuses */}
      <div className="field wide">
        <label><b>Статусы алертов</b> (ничего не отмечено = все)</label>
        <div className="chips">
          {allStatuses.map((s) => (
            <label key={s} className="chk">
              <input type="checkbox" checked={statuses.has(s)}
                     onChange={() => toggleStatus(s)} /> {s}
            </label>
          ))}
        </div>
      </div>

      {/* trigger rules */}
      <div className="field wide">
        <label><b>Правила срабатывания</b> (пусто = значение по умолчанию)</label>
        <div className="rule-grid">
          {RULE_FIELDS.map(([k, label, kind]) => (
            <div className="field" key={k}>
              <label>
                {label}{overridden.has(k) && <span className="badge"> изменено</span>}
                <span className="hint"> · по умолч. {showVal(defaults[k], kind)}</span>
              </label>
              <input value={edits[k]} disabled={busy}
                     placeholder={showVal(defaults[k], kind)}
                     onChange={(e) => setEdits((p) => ({ ...p, [k]: e.target.value }))} />
            </div>
          ))}
        </div>
      </div>

      <div className="actions">
        <button type="button" disabled={busy} onClick={saveRules}>Сохранить</button>
        <button type="button" className="tab" disabled={busy}
                onClick={() => run(() => tgResetRules(chat.chat_id))}>
          Сбросить правила
        </button>
        <span className="hint status">{status}</span>
      </div>
    </div>
  )
}

export default function TelegramChatsPanel({ active }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    try { setData(await getTelegramChats()) }
    catch (e) { setError(e.message) }
  }

  useEffect(() => { if (active) load() }, [active])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="empty">Загружаю…</div>
  if (!data.chats.length)
    return (
      <div className="empty">
        Бот пока не знает ни одного чата. Добавьте бота в чат и выполните там
        любую команду (например <code>/subscribe</code>) — он появится здесь.
      </div>
    )

  return (
    <div>
      <p className="sub">
        Чаты, которые знает Telegram-бот: подписки на проекты, статусы алертов и
        правила срабатывания. То же, что команды <code>/subscribe</code>,{' '}
        <code>/alerts</code>, <code>/set</code> в самом чате.
      </p>
      {data.chats.map((chat) => (
        <ChatCard key={chat.chat_id} chat={chat} projects={data.projects}
                  allStatuses={data.all_statuses} defaults={data.rule_defaults}
                  onChanged={load} />
      ))}
    </div>
  )
}
