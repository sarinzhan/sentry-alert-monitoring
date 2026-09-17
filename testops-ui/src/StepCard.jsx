import { useState } from 'react'
import TestItem from './TestItem.jsx'

export default function StepCard({
  step, isLast, tests, selected, outcomes, running,
  onToggle, onRunOne, onSave,
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({ title: '', description: '' })

  const startEdit = () => {
    setDraft({ title: step.title || step.slug, description: step.description })
    setEditing(true)
  }

  const save = async () => {
    await onSave({ title: draft.title, description: draft.description })
    setEditing(false)
  }

  const signalChips = Object.entries(step.signals || {}).flatMap(([kind, value]) =>
    (Array.isArray(value) ? value : [value]).map((v) => (
      <span key={`${kind}:${v}`} className="chip" title={kind}>{v}</span>
    )),
  )

  return (
    <>
      <div className="step-card">
        {editing ? (
          <div className="step-edit">
            <input
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
            <textarea
              rows={5}
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
            <div className="edit-actions">
              <button className="primary" onClick={save}>Save</button>
              <button onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <>
            <div className="step-title">
              <h3>{step.title || step.slug}</h3>
              <button className="ghost" title="Edit step" onClick={startEdit}>✎</button>
            </div>
            <p className="step-desc">{step.description || <i>no description yet</i>}</p>
            {signalChips.length > 0 && <div className="chips">{signalChips}</div>}
          </>
        )}

        <div className="tests">
          {tests.length === 0 && <div className="no-tests">no tests linked</div>}
          {tests.map((t) => (
            <TestItem
              key={t.node_id}
              test={t}
              checked={selected.has(t.node_id)}
              outcome={outcomes[t.node_id]}
              running={running}
              onToggle={() => onToggle(t.node_id)}
              onRun={() => onRunOne(t.node_id)}
            />
          ))}
        </div>
      </div>
      {!isLast && <div className="arrow">→</div>}
    </>
  )
}
