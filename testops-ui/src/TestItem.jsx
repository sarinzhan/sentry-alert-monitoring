import { useState } from 'react'

const OUTCOME_ICON = { passed: '✅', failed: '❌', error: '💥', skipped: '⏭️' }

export default function TestItem({ test, checked, outcome, running, onToggle, onRun }) {
  const [open, setOpen] = useState(false)

  return (
    <div className={`test-item ${outcome?.outcome || ''}`}>
      <div className="test-row">
        <input type="checkbox" checked={checked} onChange={onToggle} disabled={running} />
        <button className="test-name" onClick={() => setOpen(!open)} title={test.node_id}>
          {test.name}
        </button>
        {test.destructive && <span className="badge destructive" title="creates real data on stage">destructive</span>}
        {outcome && (
          <span className="outcome" title={outcome.outcome}>
            {OUTCOME_ICON[outcome.outcome] || outcome.outcome} {outcome.duration ? `${outcome.duration}s` : ''}
          </span>
        )}
        <button className="ghost" disabled={running} onClick={onRun} title="Run this test">▶</button>
      </div>
      {open && (
        <div className="test-detail">
          {test.doc && <p className="doc">{test.doc}</p>}
          {test.needs.length > 0 && (
            <ul className="needs">
              {test.needs.map((n) => <li key={n}>needs: {n}</li>)}
            </ul>
          )}
          {outcome?.message && <pre className="failure">{outcome.message}</pre>}
        </div>
      )}
    </div>
  )
}
