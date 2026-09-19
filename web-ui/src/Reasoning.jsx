import { useState } from 'react'

const clip = (s, n) => {
  s = typeof s === 'string' ? s : JSON.stringify(s)
  return s.length > n ? s.slice(0, n) + '…' : s
}

function Step({ step }) {
  if (step.type === 'status') {
    return <div className="step status-step">⏳ {step.message}</div>
  }
  if (step.type === 'text') {
    return <div className="step think">💭 {step.text}</div>
  }
  if (step.type === 'tool') {
    const name = step.tool.replace(/^mcp__\w+__/, '')
    return (
      <div className="step tool">
        🔧 <b>{name}</b><span className="args">({clip(step.args, 160)})</span>
      </div>
    )
  }
  if (step.type === 'tool_result') {
    return <div className="step tool-result">→ {clip(step.result, 220)}</div>
  }
  return null
}

// Live run: only the LAST step is shown (plus the text being typed out via
// stream deltas — `live`); «развернуть» reveals the whole trace. A finished
// run collapses into the usual <details>.
export default function Reasoning({ steps, running, usage, live }) {
  const [expanded, setExpanded] = useState(false)
  if (!steps.length && !(running && live)) return null
  const tokens = usage && (
    <div className="hint tokens">
      токены: {usage.in_tokens.toLocaleString('ru')} вх · {usage.out_tokens.toLocaleString('ru')} исх
    </div>
  )
  if (running) {
    const shown = expanded ? steps : steps.slice(-1)
    return (
      <div className="reasoning">
        {steps.length > 1 && (
          <div className="reasoning-head">
            <button type="button" className="linkish"
                    onClick={() => setExpanded(!expanded)}>
              {expanded ? 'свернуть ход рассуждений'
                        : `развернуть ход рассуждений (${steps.length} шагов)`}
            </button>
          </div>
        )}
        <div className="steps">
          {shown.map((s, i) => (
            <Step key={expanded ? i : `last-${steps.length}`} step={s} />
          ))}
          {live
            ? <div className="step live">{live}<span className="blink">▍</span></div>
            : <div className="step status-step blink">▍</div>}
        </div>
        {tokens}
      </div>
    )
  }
  if (!steps.length) return null
  return (
    <details className="reasoning">
      <summary>Ход рассуждений ({steps.length} шагов)</summary>
      <div className="steps">
        {steps.map((s, i) => <Step key={i} step={s} />)}
      </div>
      {tokens}
    </details>
  )
}
