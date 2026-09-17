import { useState } from 'react'
import InvestigateForm from './InvestigateForm.jsx'
import Results from './Results.jsx'
import { investigate } from './api.js'

export default function App() {
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function onSubmit(body) {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await investigate(body))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main>
      <h1>Расследование проблемы</h1>
      <p className="sub">Поиск ошибок и логов в Sentry по запросу, устройству или абоненту.</p>
      <InvestigateForm onSubmit={onSubmit} busy={busy} serverError={error} />
      {result && <Results data={result} />}
    </main>
  )
}
