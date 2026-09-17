export default function RunsPanel({ activeRun }) {
  if (!activeRun) return null
  const { status, report, node_ids: nodeIds } = activeRun
  const summary = report?.summary

  return (
    <section className="runs-panel">
      <h2>
        Run {activeRun.id}{' '}
        <span className={`status ${status}`}>
          {status === 'running' ? '⏳ running…' : status}
        </span>
      </h2>
      <p>{nodeIds?.length} test(s) selected</p>
      {summary && (
        <p className="summary">
          {summary.passed || 0} passed · {summary.failed || 0} failed ·{' '}
          {summary.skipped || 0} skipped · {report.duration?.toFixed?.(1)}s total
        </p>
      )}
      {report?.error && <pre className="failure">{report.error}</pre>}
    </section>
  )
}
