import { useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import axios from 'axios'
import Plot from 'react-plotly.js'

const CHANNEL_GROUPS: Record<string, string[]> = {
  'Attitude': ['ATT.roll_deg', 'ATT.pitch_deg', 'ATT.yaw_deg'],
  'EKF': ['NKF4.var_ratio_vel', 'NKF4.var_ratio_pos', 'NKF1.posD_m'],
  'GPS': ['GPS.hdop', 'GPS.sat_count', 'GPS.speed_acc_ms'],
  'Power': ['BAT.voltage_v', 'BAT.current_a', 'BAT.remaining_pct'],
  'Motors': ['RCOU.ch1_us', 'RCOU.ch2_us', 'RCOU.ch3_us', 'RCOU.ch4_us'],
  'Vibration': ['VIBE.vibe_x', 'VIBE.vibe_y', 'VIBE.vibe_z'],
  'Baro': ['BARO.alt_m', 'BARO.press_pa'],
  'IMU': ['IMU.accel_x_ms2', 'IMU.accel_y_ms2', 'IMU.accel_z_ms2'],
}

const SEV_COLOR: Record<string, string> = {
  INFO: '#58a6ff', WARNING: '#d29922', CRITICAL: '#f85149', FATAL: '#f85149',
}

const PLOT_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#79c0ff', '#56d364', '#ff7b72']

interface Anomaly {
  id: string
  timestamp_us: number
  severity: string
  category: string
  rule_name: string
  description: string
}

interface TelemetrySeries {
  channel: string
  timestamps_us: number[]
  values: number[]
}

export default function ExplorerPage() {
  const { flightId: routeFlightId } = useParams<{ flightId: string }>()
  const navigate = useNavigate()
  const [flightIdInput, setFlightIdInput] = useState(routeFlightId || '')
  const [activeFlightId, setActiveFlightId] = useState(routeFlightId || '')
  const [selectedChannels, setSelectedChannels] = useState<string[]>(['ATT.roll_deg', 'ATT.pitch_deg'])
  const [selectedGroup, setSelectedGroup] = useState<string>('Attitude')
  const [rangeUs, setRangeUs] = useState<[number, number] | null>(null)
  const [showAnomalies, setShowAnomalies] = useState(true)

  const { data: flight } = useQuery({
    queryKey: ['flight', activeFlightId],
    queryFn: () => axios.get(`/api/v1/flights/${activeFlightId}`).then(r => r.data),
    enabled: !!activeFlightId,
  })

  const { data: anomalies } = useQuery<Anomaly[]>({
    queryKey: ['anomalies', activeFlightId],
    queryFn: () => axios.get(`/api/v1/flights/${activeFlightId}/anomalies`).then(r => r.data),
    enabled: !!activeFlightId,
  })

  const { data: phases } = useQuery({
    queryKey: ['phases', activeFlightId],
    queryFn: () => axios.get(`/api/v1/flights/${activeFlightId}/phases`).then(r => r.data),
    enabled: !!activeFlightId,
  })

  const { data: telemetry, isFetching } = useQuery<TelemetrySeries[]>({
    queryKey: ['telemetry', activeFlightId, selectedChannels, rangeUs],
    queryFn: () => {
      const params: Record<string, any> = {
        channels: selectedChannels.join(','),
        max_points: 3000,
      }
      if (rangeUs) {
        params.start_us = rangeUs[0]
        params.end_us = rangeUs[1]
      }
      return axios.get(`/api/v1/telemetry/${activeFlightId}/series`, { params }).then(r => r.data)
    },
    enabled: !!activeFlightId && selectedChannels.length > 0,
  })

  const handleLoad = useCallback(() => {
    if (!flightIdInput.trim()) return
    setActiveFlightId(flightIdInput.trim())
    navigate(`/explore/${flightIdInput.trim()}`)
  }, [flightIdInput, navigate])

  const toggleChannel = (ch: string) => {
    setSelectedChannels(prev =>
      prev.includes(ch) ? prev.filter(c => c !== ch) : [...prev, ch]
    )
  }

  const selectGroup = (group: string) => {
    setSelectedGroup(group)
    setSelectedChannels(CHANNEL_GROUPS[group])
  }

  const buildTraces = (): Plotly.Data[] => {
    if (!telemetry) return []
    return telemetry.map((series, i) => ({
      x: series.timestamps_us.map(t => t / 1e6),
      y: series.values,
      type: 'scatter' as const,
      mode: 'lines' as const,
      name: series.channel,
      line: { color: PLOT_COLORS[i % PLOT_COLORS.length], width: 1.5 },
    }))
  }

  const buildAnomalyShapes = (): Partial<Plotly.Shape>[] => {
    if (!showAnomalies || !anomalies) return []
    return anomalies.map(a => ({
      type: 'line' as const,
      x0: a.timestamp_us / 1e6,
      x1: a.timestamp_us / 1e6,
      y0: 0,
      y1: 1,
      yref: 'paper' as const,
      line: {
        color: SEV_COLOR[a.severity] || '#8b949e',
        width: 1,
        dash: 'dot' as const,
      },
    }))
  }

  const buildPhaseShapes = (): Partial<Plotly.Shape>[] => {
    if (!phases?.phases) return []
    const phaseColors: Record<string, string> = {
      TAKEOFF: 'rgba(63,185,80,0.06)',
      HOVER: 'rgba(88,166,255,0.04)',
      AUTO_MISSION: 'rgba(188,140,255,0.06)',
      RTL: 'rgba(210,153,34,0.08)',
      LANDING: 'rgba(248,81,73,0.06)',
      PRE_ARM: 'rgba(139,148,158,0.04)',
    }
    return (phases.phases as any[]).map(p => ({
      type: 'rect' as const,
      x0: p.start_us / 1e6,
      x1: p.end_us / 1e6,
      y0: 0,
      y1: 1,
      yref: 'paper' as const,
      fillcolor: phaseColors[p.name] || 'rgba(255,255,255,0.02)',
      line: { width: 0 },
    }))
  }

  const plotLayout: Partial<Plotly.Layout> = {
    paper_bgcolor: '#0d1117',
    plot_bgcolor: '#161b22',
    font: { color: '#e6edf3', family: 'monospace', size: 11 },
    margin: { t: 20, r: 20, b: 40, l: 60 },
    xaxis: {
      title: 'Time (s)',
      color: '#8b949e',
      gridcolor: '#21262d',
      zerolinecolor: '#30363d',
    },
    yaxis: {
      color: '#8b949e',
      gridcolor: '#21262d',
      zerolinecolor: '#30363d',
    },
    legend: {
      bgcolor: 'rgba(22,27,34,0.8)',
      bordercolor: '#30363d',
      borderwidth: 1,
      font: { size: 10 },
    },
    shapes: [...buildPhaseShapes(), ...buildAnomalyShapes()],
    hovermode: 'x unified' as const,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 90px)', gap: 12 }}>
      {/* Top bar */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flex: 1, minWidth: 300 }}>
          <input
            value={flightIdInput}
            onChange={e => setFlightIdInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleLoad()}
            placeholder="Flight ID (UUID)..."
            style={{ flex: 1, padding: '6px 12px', background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 12, fontFamily: 'monospace' }}
          />
          <button
            onClick={handleLoad}
            style={{ padding: '6px 16px', background: '#238636', border: 'none', borderRadius: 6, color: '#fff', fontSize: 12, cursor: 'pointer', fontFamily: 'monospace' }}
          >
            Load
          </button>
        </div>

        {flight && (
          <div style={{ display: 'flex', gap: 16, color: '#8b949e', fontSize: 11, fontFamily: 'monospace' }}>
            <span style={{ color: '#3fb950' }}>{flight.filename}</span>
            <span>{flight.format?.toUpperCase()} · {flight.autopilot?.toUpperCase()}</span>
            {flight.duration_s && <span>{Math.round(flight.duration_s)}s flight</span>}
            <span style={{ color: flight.status === 'ready' ? '#3fb950' : '#d29922' }}>{flight.status?.toUpperCase()}</span>
          </div>
        )}

        <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#8b949e', fontSize: 11, cursor: 'pointer' }}>
          <input type="checkbox" checked={showAnomalies} onChange={e => setShowAnomalies(e.target.checked)} />
          Show anomalies
        </label>
      </div>

      <div style={{ display: 'flex', flex: 1, gap: 12, minHeight: 0 }}>
        {/* Left: Channel selector */}
        <div style={{ width: 220, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'auto' }}>
          <div style={{ color: '#8b949e', fontSize: 11 }}>CHANNEL GROUPS</div>
          {Object.keys(CHANNEL_GROUPS).map(group => (
            <div key={group}>
              <button
                onClick={() => selectGroup(group)}
                style={{
                  width: '100%', textAlign: 'left', padding: '4px 8px',
                  background: selectedGroup === group ? 'rgba(88,166,255,0.1)' : 'transparent',
                  border: `1px solid ${selectedGroup === group ? '#58a6ff' : '#30363d'}`,
                  borderRadius: 4, color: selectedGroup === group ? '#58a6ff' : '#8b949e',
                  fontSize: 11, cursor: 'pointer', fontFamily: 'monospace',
                }}
              >
                {group}
              </button>
              {selectedGroup === group && (
                <div style={{ paddingLeft: 8, paddingTop: 4, display: 'flex', flexDirection: 'column', gap: 2 }}>
                  {CHANNEL_GROUPS[group].map((ch, i) => (
                    <label key={ch} style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={selectedChannels.includes(ch)}
                        onChange={() => toggleChannel(ch)}
                      />
                      <span style={{ color: selectedChannels.includes(ch) ? PLOT_COLORS[i % PLOT_COLORS.length] : '#8b949e', fontSize: 11 }}>
                        {ch}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          ))}

          {/* Anomaly list */}
          {anomalies && anomalies.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ color: '#8b949e', fontSize: 11, marginBottom: 6 }}>ANOMALIES ({anomalies.length})</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 300, overflow: 'auto' }}>
                {anomalies.slice(0, 50).map(a => (
                  <div
                    key={a.id}
                    onClick={() => {
                      const t = a.timestamp_us / 1e6
                      setRangeUs([a.timestamp_us - 10_000_000, a.timestamp_us + 10_000_000])
                    }}
                    style={{
                      padding: '4px 6px', background: '#161b22', border: `1px solid ${SEV_COLOR[a.severity] || '#30363d'}33`,
                      borderRadius: 4, cursor: 'pointer', fontSize: 10,
                    }}
                  >
                    <div style={{ color: SEV_COLOR[a.severity] || '#8b949e' }}>
                      {a.severity} · {(a.timestamp_us / 1e6).toFixed(1)}s
                    </div>
                    <div style={{ color: '#8b949e', marginTop: 2 }}>{a.rule_name}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right: Plot area */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
          {!activeFlightId ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b949e', fontSize: 13 }}>
              Enter a flight ID to explore telemetry
            </div>
          ) : isFetching ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#d29922', fontSize: 13 }}>
              Loading telemetry...
            </div>
          ) : telemetry && telemetry.length > 0 ? (
            <>
              {/* Phase legend */}
              {phases?.phases && (
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', paddingBottom: 4 }}>
                  {(phases.phases as any[]).map((p: any, i: number) => (
                    <span
                      key={i}
                      onClick={() => setRangeUs([p.start_us, p.end_us])}
                      style={{ fontSize: 10, color: '#8b949e', cursor: 'pointer', textDecoration: 'underline dotted' }}
                    >
                      {p.name} ({(p.start_us / 1e6).toFixed(0)}s–{(p.end_us / 1e6).toFixed(0)}s)
                    </span>
                  ))}
                  {rangeUs && (
                    <span
                      onClick={() => setRangeUs(null)}
                      style={{ fontSize: 10, color: '#58a6ff', cursor: 'pointer' }}
                    >
                      ✕ clear range
                    </span>
                  )}
                </div>
              )}

              <div style={{ flex: 1, minHeight: 0 }}>
                <Plot
                  data={buildTraces()}
                  layout={{
                    ...plotLayout,
                    shapes: [...buildPhaseShapes(), ...buildAnomalyShapes()],
                  }}
                  style={{ width: '100%', height: '100%' }}
                  config={{ responsive: true, displayModeBar: true, displaylogo: false }}
                  onRelayout={(e: any) => {
                    if (e['xaxis.range[0]'] !== undefined && e['xaxis.range[1]'] !== undefined) {
                      setRangeUs([Math.round(e['xaxis.range[0]'] * 1e6), Math.round(e['xaxis.range[1]'] * 1e6)])
                    }
                    if (e['xaxis.autorange']) setRangeUs(null)
                  }}
                />
              </div>

              {/* Stats bar */}
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', padding: '6px 0', borderTop: '1px solid #21262d' }}>
                {telemetry.map((s, i) => {
                  const vals = s.values.filter(v => v !== null && !isNaN(v))
                  if (!vals.length) return null
                  const min = Math.min(...vals)
                  const max = Math.max(...vals)
                  const mean = vals.reduce((a, b) => a + b, 0) / vals.length
                  return (
                    <div key={s.channel} style={{ fontSize: 10, fontFamily: 'monospace' }}>
                      <span style={{ color: PLOT_COLORS[i % PLOT_COLORS.length] }}>{s.channel}: </span>
                      <span style={{ color: '#8b949e' }}>
                        min={min.toFixed(2)} max={max.toFixed(2)} mean={mean.toFixed(2)}
                      </span>
                    </div>
                  )
                })}
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b949e', fontSize: 13 }}>
              {flight?.status !== 'ready'
                ? `Flight is ${flight?.status || 'loading'} — telemetry not available yet`
                : 'No data for selected channels'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
