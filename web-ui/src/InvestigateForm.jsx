import { useEffect, useState } from 'react'
import { getMeta, getPrompts } from './api.js'

export const PERIODS = [
  ['1h', 'последний час'],
  ['24h', 'последние 24 часа'],
  ['3d', 'последние 3 дня'],
  ['7d', 'последние 7 дней'],
  ['14d', 'последние 14 дней'],
  ['30d', 'последний месяц'],
  ['custom', 'свои даты'],
]

const EMPTY = {
  request_id: '',
  device_id: '',
  msisdn: '',
  period: '3d',
  date_from: '',
  date_to: '',
  environment: '',
  description: '',
  role_id: '',
  model: '',
}

export default function InvestigateForm({ onSubmit, busy, serverError }) {
  const [values, setValues] = useState(EMPTY)
  const [touched, setTouched] = useState(false)
  const [meta, setMeta] = useState({ environments: [], tz_offset_hours: null,
                                     models: [], default_model: '' })
  const [prompts, setPrompts] = useState({ roles: [], problems: [] })
  const [template, setTemplate] = useState('')

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {})
    getPrompts().then(setPrompts).catch(() => {})
  }, [])

  // a problem template prefills the description; the user then adds details
  function applyTemplate(e) {
    const id = e.target.value
    setTemplate(id)
    const p = prompts.problems.find((x) => String(x.id) === id)
    if (p) setValues((v) => ({ ...v, description: p.text }))
  }

  const hasId = ['request_id', 'device_id', 'msisdn'].some((k) => values[k].trim())
  const hasDescription = Boolean(values.description.trim())
  const custom = values.period === 'custom'
  const datesOk = !custom || (values.date_from && values.date_to &&
    values.date_from <= values.date_to)
  const valid = hasId && hasDescription && datesOk

  function set(name) {
    return (e) => setValues((v) => ({ ...v, [name]: e.target.value }))
  }

  function submit(e) {
    e.preventDefault()
    setTouched(true)
    if (!valid || busy) return
    const body = {}
    for (const [k, v] of Object.entries(values)) body[k] = v.trim()
    if (custom) delete body.period
    else { delete body.date_from; delete body.date_to }
    onSubmit(body)
  }

  const idInvalid = touched && !hasId
  const tz = meta.tz_offset_hours
  const tzHint = tz == null ? '' : ` (местные, UTC${tz >= 0 ? '+' : ''}${tz})`
  const validationError = touched && !valid
    ? (!datesOk && hasId && hasDescription
        ? 'Укажите корректный диапазон дат («с» не позже «по»).'
        : 'Заполните описание и хотя бы одно из: Request ID, Device ID, номер.')
    : ''

  return (
    <form onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="request_id">Request ID</label>
        <input id="request_id" value={values.request_id} onChange={set('request_id')}
               className={idInvalid ? 'invalid' : ''} autoComplete="off"
               placeholder="7f3a9c12…" />
      </div>
      <div className="field">
        <label htmlFor="device_id">Device ID</label>
        <input id="device_id" value={values.device_id} onChange={set('device_id')}
               className={idInvalid ? 'invalid' : ''} autoComplete="off" />
      </div>
      <div className="field">
        <label htmlFor="msisdn">Номер (msisdn)</label>
        <input id="msisdn" value={values.msisdn} onChange={set('msisdn')}
               className={idInvalid ? 'invalid' : ''} autoComplete="off"
               placeholder="996555123456" />
      </div>
      <div className="field">
        <label htmlFor="period">Период поиска</label>
        <select id="period" value={values.period} onChange={set('period')}>
          {PERIODS.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </div>
      {custom && (
        <>
          <div className="field">
            <label htmlFor="date_from">С даты<span className="hint">{tzHint}</span></label>
            <input id="date_from" type="date" value={values.date_from}
                   onChange={set('date_from')} max={values.date_to || undefined}
                   className={touched && !datesOk ? 'invalid' : ''} />
          </div>
          <div className="field">
            <label htmlFor="date_to">По дату (включительно)</label>
            <input id="date_to" type="date" value={values.date_to}
                   onChange={set('date_to')} min={values.date_from || undefined}
                   className={touched && !datesOk ? 'invalid' : ''} />
          </div>
        </>
      )}
      <div className="field">
        <label htmlFor="environment">Среда</label>
        <select id="environment" value={values.environment} onChange={set('environment')}>
          <option value="">все среды</option>
          {meta.environments.map((env) => (
            <option key={env} value={env}>{env}</option>
          ))}
        </select>
      </div>
      {prompts.problems.length > 0 && (
        <div className="field">
          <label htmlFor="template">Шаблон проблемы</label>
          <select id="template" value={template} onChange={applyTemplate}>
            <option value="">— не использовать —</option>
            {prompts.problems.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label htmlFor="role_id">Ответ для</label>
        <select id="role_id" value={values.role_id} onChange={set('role_id')}>
          <option value="">стандартный (поддержка)</option>
          {prompts.roles.map((r) => (
            <option key={r.id} value={r.id}>{r.name}</option>
          ))}
        </select>
      </div>
      {meta.models.length > 0 && (
        <div className="field">
          <label htmlFor="model">Модель ИИ</label>
          <select id="model" value={values.model} onChange={set('model')}>
            <option value="">{`по умолчанию (${meta.default_model})`}</option>
            {meta.models.filter((m) => m !== meta.default_model).map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
      )}
      <div className="field wide">
        <label htmlFor="description">
          <b>Описание проблемы</b> <span className="req">*</span>
        </label>
        <textarea id="description" value={values.description} onChange={set('description')}
                  className={touched && !hasDescription ? 'invalid' : ''}
                  placeholder="Например: не смог подключить пакет, приложение показало ошибку" />
      </div>
      <div className="actions">
        <button type="submit" disabled={busy}>{busy ? 'Ищу…' : 'Найти'}</button>
        <span className="error">{validationError || serverError}</span>
      </div>
    </form>
  )
}
