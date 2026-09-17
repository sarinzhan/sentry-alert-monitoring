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

export default function Reasoning({ steps, running }) {
  if (!steps.length) return null
  const body = (
    <div className="steps">
      {steps.map((s, i) => <Step key={i} step={s} />)}
      {running && <div className="step status-step blink">▍</div>}
    </div>
  )
  if (running) {
    return <div className="reasoning">{body}</div>
  }
  return (
    <details className="reasoning">
      <summary>Ход рассуждений ({steps.length} шагов)</summary>
      {body}
    </details>
  )
}
