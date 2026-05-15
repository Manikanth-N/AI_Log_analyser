import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'

const ACCEPTED = '.bin,.ulog,.tlog,.csv,.json'
const SEVERITY_COLOR = { INFO: '#58a6ff', WARNING: '#d29922', CRITICAL: '#f85149', FATAL: '#f85149' }

export default function UploadPage() {
  const navigate = useNavigate()
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [flights, setFlights] = useState<any[]>([])

  const upload = async (file: File) => {
    setUploading(true)
    setError(null)
    setProgress(0)
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await axios.post('/api/v1/flights/upload', form, {
        onUploadProgress: (e) => setProgress(Math.round((e.loaded / (e.total || 1)) * 100)),
      })
      const { flight_id } = res.data
      // Immediately start investigation
      const inv = await axios.post('/api/v1/investigations', {
        flight_id,
        query: 'Perform complete forensic investigation of this flight log',
      })
      navigate(`/investigate/${inv.data.investigation_id}`)
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setUploading(false)
    }
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) upload(file)
  }, [])

  return (
    <div style={{ maxWidth: 700, margin: '0 auto', paddingTop: 48 }}>
      <h1 style={{ color: '#58a6ff', fontSize: 24, marginBottom: 8 }}>UAV Log Forensic Investigator</h1>
      <p style={{ color: '#8b949e', marginBottom: 32, fontSize: 13 }}>
        Upload an ArduPilot .BIN, PX4 .ULOG, MAVLink .TLOG, or CSV/JSON telemetry log.
        The AI will autonomously investigate it like a senior flight test engineer.
      </p>

      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => document.getElementById('file-input')?.click()}
        style={{
          border: `2px dashed ${dragging ? '#58a6ff' : '#30363d'}`,
          borderRadius: 8,
          padding: '48px 24px',
          textAlign: 'center',
          cursor: 'pointer',
          background: dragging ? 'rgba(88,166,255,0.05)' : 'transparent',
          transition: 'all 0.2s',
        }}
      >
        <input id="file-input" type="file" accept={ACCEPTED} style={{ display: 'none' }}
          onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <div style={{ fontSize: 48, marginBottom: 12 }}>✈</div>
        <div style={{ color: '#e6edf3', marginBottom: 8 }}>
          {uploading ? `Uploading... ${progress}%` : 'Drop log file here or click to browse'}
        </div>
        <div style={{ color: '#8b949e', fontSize: 12 }}>
          Supports .BIN .ULOG .TLOG .CSV .JSON — up to 6GB
        </div>
      </div>

      {error && (
        <div style={{ marginTop: 16, padding: 12, background: 'rgba(248,81,73,0.1)', border: '1px solid #f85149', borderRadius: 6, color: '#f85149', fontSize: 13 }}>
          {error}
        </div>
      )}

      {uploading && (
        <div style={{ marginTop: 12, height: 4, background: '#21262d', borderRadius: 2 }}>
          <div style={{ height: '100%', width: `${progress}%`, background: '#58a6ff', borderRadius: 2, transition: 'width 0.3s' }} />
        </div>
      )}

      <div style={{ marginTop: 48 }}>
        <h2 style={{ color: '#8b949e', fontSize: 14, marginBottom: 16 }}>INVESTIGATION CAPABILITIES</h2>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {[
            ['[EKF]', 'Filter health, innovation ratios, lane switches'],
            ['[GPS]', 'Integrity scoring, glitch detection, spoofing'],
            ['[POWER]', 'Brownout detection, internal resistance, sag'],
            ['[VIB]', 'FFT analysis, motor harmonics, clip detection'],
            ['[ESC]', 'Motor imbalance, desync, thrust asymmetry'],
            ['[MISSION]', 'Failsafe verification, RTL behavior, waypoints'],
            ['[DYNAMICS]', 'Control stability, oscillation, PID integrity'],
            ['[PARAMS]', 'Parameter compliance, drift detection'],
          ].map(([label, desc]) => (
            <div key={label} style={{ padding: 12, background: '#161b22', border: '1px solid #30363d', borderRadius: 6 }}>
              <span style={{ color: '#58a6ff', fontSize: 12, fontWeight: 700 }}>{label}</span>
              <div style={{ color: '#8b949e', fontSize: 12, marginTop: 4 }}>{desc}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
