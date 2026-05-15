import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import UploadPage from './pages/Upload'
import InvestigationPage from './pages/Investigation'
import ExplorerPage from './pages/Explorer'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: '#0d1117', color: '#e6edf3', fontFamily: 'monospace' }}>
          <nav style={{ padding: '12px 24px', background: '#161b22', borderBottom: '1px solid #30363d', display: 'flex', gap: 24, alignItems: 'center' }}>
            <span style={{ color: '#58a6ff', fontWeight: 700, fontSize: 16 }}>✈ FORENSIC-FLIGHT-AI</span>
            {[
              { to: '/', label: 'Upload' },
              { to: '/investigate', label: 'Investigation' },
              { to: '/explore', label: 'Telemetry Explorer' },
            ].map(({ to, label }) => (
              <NavLink key={to} to={to} style={({ isActive }) => ({
                color: isActive ? '#58a6ff' : '#8b949e',
                textDecoration: 'none',
                fontSize: 13,
              })}>
                {label}
              </NavLink>
            ))}
          </nav>
          <main style={{ flex: 1, overflow: 'auto', padding: 24 }}>
            <Routes>
              <Route path="/" element={<UploadPage />} />
              <Route path="/investigate/:investigationId?" element={<InvestigationPage />} />
              <Route path="/explore/:flightId?" element={<ExplorerPage />} />
            </Routes>
          </main>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
