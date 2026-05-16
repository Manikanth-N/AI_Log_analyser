import { useState, useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'

const ACCEPTED = '.bin,.ulg,.tlog,.log,.csv,.json'

export default function UploadPage() {
  const navigate = useNavigate()
  const [uploadMode, setUploadMode] = useState<'direct' | 'gcs' | null>(null)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [status, setStatus] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    axios.get('/api/v1/capabilities').then(r => setUploadMode(r.data.upload_mode)).catch(() => setUploadMode('direct'))
  }, [])

  const uploadDirect = async (file: File) => {
    setStatus('Uploading...')
    const form = new FormData()
    form.append('file', file)
    const res = await axios.post('/api/v1/flights/upload', form, {
      onUploadProgress: (e) => setProgress(Math.round((e.loaded / (e.total || 1)) * 100)),
    })
    return res.data.flight_id as string
  }

  const uploadGCS = async (file: File) => {
    // Step 1: initialise upload session
    setStatus('Initialising upload...')
    const initRes = await axios.post('/api/v1/flights/upload/init', {
      filename: file.name,
      content_type: file.type || 'application/octet-stream',
      file_size: file.size,
    })
    const { upload_url, flight_id } = initRes.data

    // Step 2: upload file directly to GCS
    setStatus('Uploading to storage...')
    await axios.put(upload_url, file, {
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      onUploadProgress: (e) => setProgress(Math.round((e.loaded / (e.total || 1)) * 100)),
    })

    // Step 3: trigger parsing
    setStatus('Processing...')
    await axios.post(`/api/v1/flights/${flight_id}/process`)
    return flight_id as string
  }

  const upload = async (file: File) => {
    setUploading(true)
    setError(null)
    setProgress(0)
    try {
      const flight_id = uploadMode === 'gcs' ? await uploadGCS(file) : await uploadDirect(file)
      // Start investigation
      setStatus('Starting investigation...')
      const inv = await axios.post('/api/v1/investigations', {
        flight_id,
        query: 'Perform complete forensic investigation of this flight log',
      })
      navigate(`/investigate/${inv.data.investigation_id}`)
    } catch (e: any) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setUploading(false)
      setStatus('')
    }
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) upload(file)
  }, [uploadMode])

  return (
    <div style={{ maxWidth: 700, margin: '0 auto', paddingTop: 48 }}>
      <h1 style={{ color: '#58a6ff', fontSize: 24, marginBottom: 8 }}>UAV Log Forensic Investigator</h1>
      <p style={{ color: '#8b949e', marginBottom: 32, fontSize: 13 }}>
        Upload an ArduPilot .BIN, PX4 .ULG, MAVLink .TLOG, or CSV/JSON telemetry log.
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
          cursor: uploadMode ? 'pointer' : 'default',
          background: dragging ? 'rgba(88,166,255,0.05)' : 'transparent',
          transition: 'all 0.2s',
        }}
      >
        <input id="file-input" type="file" accept={ACCEPTED} style={{ display: 'none' }}
          onChange={(e) => { if (!uploadMode) return; const f = e.target.files?.[0]; if (f) upload(f) }} />
        <div style={{ fontSize: 48, marginBottom: 12 }}>✈</div>
        <div style={{ color: '#e6edf3', marginBottom: 8 }}>
          {uploading
            ? `${status} ${progress > 0 ? `${progress}%` : ''}`
            : uploadMode
              ? 'Drop log file here or click to browse'
              : 'Loading...'}
        </div>
        <div style={{ color: '#8b949e', fontSize: 12 }}>
          Supports .BIN .ULG .TLOG .LOG .CSV .JSON — up to 6GB
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
