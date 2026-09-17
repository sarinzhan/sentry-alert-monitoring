import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import StepCard from './StepCard.jsx'
import RunsPanel from './RunsPanel.jsx'

export default function App() {
  const [flows, setFlows] = useState([])
  const [tests, setTests] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [outcomes, setOutcomes] = useState({}) // node_id -> {outcome, message, duration}
  const [activeRun, setActiveRun] = useState(null)
  const [error, setError] = useState('')
  const pollRef = useRef(null)

  const load = useCallback(async () => {
    try {
      setError('')
      // /api/tests auto-registers new flow/step slugs, so fetch it first
      const testList = await api.getTests()
      setTests(testList)
      setFlows(await api.getFlows())
    } catch (e) {
      setError(String(e.message || e))
    }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => () => clearInterval(pollRef.current), [])

  const pollRun = (id) => {
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const run = await api.getRun(id)
        setActiveRun(run)
        if (run.status !== 'running') {
          clearInterval(pollRef.current)
          if (run.report?.tests) {
            setOutcomes((prev) => {
              const next = { ...prev }
              for (const t of run.report.tests) next[t.node_id] = t
              return next
            })
          }
        }
      } catch (e) {
        clearInterval(pollRef.current)
        setError(String(e.message || e))
      }
    }, 2000)
  }

  const run = async (nodeIds) => {
    if (!nodeIds.length) return
    try {
      setError('')
      const { id } = await api.startRun(nodeIds)
      setActiveRun({ id, status: 'running', node_ids: nodeIds })
      pollRun(id)
    } catch (e) {
      setError(String(e.message || e))
    }
  }

  const toggle = (nodeId) => {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(nodeId) ? next.delete(nodeId) : next.add(nodeId)
      return next
    })
  }

  const saveStep = async (flowSlug, stepSlug, patch) => {
    await api.updateStep(flowSlug, stepSlug, patch)
    setFlows(await api.getFlows())
  }

  const testsFor = (flowSlug, stepSlug) =>
    tests.filter((t) => t.flow === flowSlug && t.step === stepSlug)
  const unassigned = tests.filter((t) => !t.flow)
  const running = activeRun?.status === 'running'

  return (
    <div className="app">
      <header>
        <h1>Stage TestOps</h1>
        <div className="toolbar">
          <button onClick={load} disabled={running}>↻ Refresh</button>
          <button
            className="primary"
            disabled={running || selected.size === 0}
            onClick={() => run([...selected])}
          >
            ▶ Run selected ({selected.size})
          </button>
          <button
            disabled={running || tests.length === 0}
            onClick={() => run(tests.filter((t) => !t.destructive).map((t) => t.node_id))}
          >
            ▶ Run all safe
          </button>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      {flows.map((flow) => (
        <section key={flow.slug} className="flow">
          <div className="flow-head">
            <h2>{flow.title || flow.slug}</h2>
            <p>{flow.description}</p>
          </div>
          <div className="steps">
            {flow.steps.map((step, i) => (
              <StepCard
                key={step.slug}
                step={step}
                isLast={i === flow.steps.length - 1}
                tests={testsFor(flow.slug, step.slug)}
                selected={selected}
                outcomes={outcomes}
                running={running}
                onToggle={toggle}
                onRunOne={(nodeId) => run([nodeId])}
                onSave={(patch) => saveStep(flow.slug, step.slug, patch)}
              />
            ))}
          </div>
        </section>
      ))}

      {unassigned.length > 0 && (
        <section className="flow">
          <div className="flow-head"><h2>Tests without a flow</h2></div>
          <ul className="plain-tests">
            {unassigned.map((t) => <li key={t.node_id}>{t.node_id}</li>)}
          </ul>
        </section>
      )}

      <RunsPanel activeRun={activeRun} />
    </div>
  )
}
