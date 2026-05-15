import { useParams } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import axios from 'axios'

const SEV_COLOR: Record<string, string> = {
  INFO: '#58a6ff', WARNING: '#d29922', CRITICAL: '#f85149', FATAL: '#f85149',
}
const AGENT_ICONS: Record<string, string> = {
  FlightTimelineAgent: '📅', EKFDiagnosticsAgent: '🧭', GPSIntegrityAgent: '📡',
  PowerSystemAgent: '🔋', VibrationAnalysisAgent: '📳', ESCMotorAgent: '⚙️',
  MissionBehaviorAgent: '🗺️', FlightDynamicsAgent: '✈️', ParameterDriftAgent: '⚡',
  CrashInvestigatorAgent: '🔍', ReportWriterAgent: '📝',
}

interface AgentMessage { agent: string; level: string; message: string; timestamp: number }

export default function InvestigationPage() {
  const { investigationId } = useParams<{ investigationId: string }>()
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [complete, setComplete] = useState(false)
  const feedRef = useRef<HTMLDivElement>(null)

  const { data: inv } = useQuery({
    queryKey: ['investigation', investigationId],
    queryFn: () => axios.get(`/api/v1/investigations/${investigationId}`).then(r => r.data),
    enabled: !!investigationId && complete,
    refetchInterval: complete ? false : 3000,
  })

  // SSE stream
  useEffect(() => {
    if (!investigationId) return
    const es = new EventSource(`/api/v1/investigations/${investigationId}/stream`)

    es.addEventListener('update', (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.agent && data.message) {
          setMessages(m => [...m, data])
        }
      } catch {}
    })

    es.addEventListener('complete', () => { setComplete(true); es.close() })
    es.addEventListener('error', () => { setComplete(true); es.close() })

    return () => es.close()
  }, [investigationId])

  // Auto-scroll feed
  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  if (!investigationId) return <div style={{ color: '#8b949e' }}>No investigation selected</div>

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, height: 'calc(100vh - 90px)' }}>
      {/* Left: Agent Activity Feed */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <h2 style={{ color: '#58a6ff', fontSize: 14, margin: 0 }}>
          AGENT INVESTIGATION FEED {!complete && <span style={{ color: '#d29922' }}>● LIVE</span>}
        </h2>
        <div
          ref={feedRef}
          style={{ flex: 1, background: '#161b22', border: '1px solid #30363d', borderRadius: 8, padding: 12, overflow: 'auto', fontFamily: 'monospace', fontSize: 12 }}
        >
          {messages.map((m, i) => (
            <div key={i} style={{ marginBottom: 6, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <span style={{ color: '#8b949e', minWidth: 60 }}>
                {new Date(m.timestamp * 1000).toLocaleTimeString()}
              </span>
              <span style={{ minWidth: 20 }}>{AGENT_ICONS[m.agent] || '•'}</span>
              <span style={{ color: '#8b949e', minWidth: 180, fontSize: 11 }}>{m.agent}</span>
              <span style={{ color: '#e6edf3' }}>{m.message}</span>
            </div>
          ))}
          {!complete && <div style={{ color: '#d29922' }}>▸ Investigating...</div>}
          {complete && <div style={{ color: '#3fb950', marginTop: 8 }}>✓ Investigation complete</div>}
        </div>
      </div>

      {/* Right: Results */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflow: 'auto' }}>
        <h2 style={{ color: '#58a6ff', fontSize: 14, margin: 0 }}>FINDINGS</h2>

        {inv ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {/* Root Cause */}
            <div style={{ padding: 16, background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}>
              <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 8 }}>ROOT CAUSE</div>
              <div style={{ color: '#f85149', fontSize: 13 }}>{inv.root_cause || 'Analyzing...'}</div>
            </div>

            {/* Contributing Factors */}
            {inv.contributing_factors?.length > 0 && (
              <div style={{ padding: 16, background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}>
                <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 8 }}>CONTRIBUTING FACTORS</div>
                {inv.contributing_factors.map((f: string, i: number) => (
                  <div key={i} style={{ color: '#d29922', fontSize: 12, marginBottom: 4 }}>• {f}</div>
                ))}
              </div>
            )}

            {/* Recommendations */}
            {inv.recommendations?.length > 0 && (
              <div style={{ padding: 16, background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}>
                <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 8 }}>CORRECTIVE ACTIONS</div>
                {inv.recommendations.map((r: string, i: number) => (
                  <div key={i} style={{ color: '#3fb950', fontSize: 12, marginBottom: 4 }}>→ {r}</div>
                ))}
              </div>
            )}

            {/* Agent Findings Summary */}
            {inv.agent_findings && Object.keys(inv.agent_findings).length > 0 && (
              <div style={{ padding: 16, background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}>
                <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 8 }}>AGENT SUMMARIES</div>
                {Object.entries(inv.agent_findings as Record<string, any>).map(([agent, findings]) => (
                  <div key={agent} style={{ marginBottom: 8, paddingBottom: 8, borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#58a6ff', fontSize: 11 }}>{AGENT_ICONS[agent]} {agent}</span>
                    <div style={{ color: '#8b949e', fontSize: 11, marginTop: 4 }}>
                      {(findings as any).summary || ''}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Follow-up Questions */}
            <FollowUpPanel investigationId={investigationId} />
          </div>
        ) : (
          <div style={{ color: '#8b949e', fontSize: 13 }}>Waiting for investigation to complete...</div>
        )}
      </div>
    </div>
  )
}

function FollowUpPanel({ investigationId }: { investigationId: string }) {
  const [question, setQuestion] = useState('')
  const [answers, setAnswers] = useState<Array<{ q: string; a: string }>>([])
  const [loading, setLoading] = useState(false)

  const ask = async () => {
    if (!question.trim()) return
    setLoading(true)
    try {
      const res = await axios.post(`/api/v1/investigations/${investigationId}/query`, { question })
      setAnswers(prev => [...prev, { q: question, a: res.data.answer }])
      setQuestion('')
    } catch (e: any) {
      setAnswers(prev => [...prev, { q: question, a: `Error: ${e.message}` }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ padding: 16, background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}>
      <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 12 }}>FOLLOW-UP QUESTIONS</div>
      <div style={{ maxHeight: 200, overflow: 'auto', marginBottom: 12 }}>
        {answers.map((a, i) => (
          <div key={i} style={{ marginBottom: 12 }}>
            <div style={{ color: '#58a6ff', fontSize: 12 }}>Q: {a.q}</div>
            <div style={{ color: '#e6edf3', fontSize: 12, marginTop: 4, paddingLeft: 8 }}>{a.a}</div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && ask()}
          placeholder="Was GPS spoofing likely? Did EKF fail? Why did it crash?"
          style={{ flex: 1, padding: '8px 12px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 12 }}
        />
        <button
          onClick={ask}
          disabled={loading}
          style={{ padding: '8px 16px', background: '#238636', border: 'none', borderRadius: 6, color: '#fff', fontSize: 12, cursor: 'pointer' }}
        >
          {loading ? '...' : 'Ask'}
        </button>
      </div>
    </div>
  )
}
