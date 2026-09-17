import { useEffect, useState } from 'react'
import { getMeta } from './api.js'

const EMPTY = {
  request_id: '',
  device_id: '',
  msisdn: '',
  when_local: '',
  environment: '',
  description: '',
}

export default function InvestigateForm({ onSubmit, busy, serverError }) {
  const [values, setValues] = useState(EMPTY)
  const [touched, setTouched] = useState(false)
  const [meta, setMeta] = useState({ environments: [], tz_offset_hours: null })

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {})
  }, [])

  const hasId = ['request_id', 'device_id', 'msisdn'].some((k) => values[k].trim())
  const hasDescription = Boolean(values.description.trim())
  const valid = hasId && hasDescription

  function set(name) {
    return (e) => setValues((v) => ({ ...v, [name]: e.target.value }))
  }

  function submit(e) {
    e.preventDefault()
    setTouched(true)
    if (!valid || busy) return
    const body = {}
    for (const [k, v] of Object.entries(values)) body[k] = v.trim()
    onSubmit(body)
  }

  const idInvalid = touched && !hasId
  const tz = meta.tz_offset_hours
  const tzHint = tz == null ? '' : ` (местное, UTC${tz >= 0 ? '+' : ''}${tz})`
  const validationError = touched && !valid
    ? 'Заполните описание и хотя бы одно из: Request ID, Device ID, номер.'
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
        <label htmlFor="when_local">Примерное время<span className="hint">{tzHint}</span></label>
        <input id="when_local" type="datetime-local" value={values.when_local}
               onChange={set('when_local')} />
        <span className="hint">пусто — поиск за последние 24 часа</span>
      </div>
      <div className="field">
        <label htmlFor="environment">Среда</label>
        <select id="environment" value={values.environment} onChange={set('environment')}>
          <option value="">все среды</option>
          {meta.environments.map((env) => (
            <option key={env} value={env}>{env}</option>
          ))}
        </select>
      </div>
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
