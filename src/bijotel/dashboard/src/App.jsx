import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import ChainExplorer from './pages/ChainExplorer.jsx'

// Day 9 will replace these stubs with real pages.
function PolicyDashboardStub() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold mb-2">Policy Decisions</h1>
      <p className="text-gray-500">Coming in Day 9.</p>
    </div>
  )
}
function RegressionViewStub() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold mb-2">Regression Monitor</h1>
      <p className="text-gray-500">Coming in Day 9.</p>
    </div>
  )
}
function SystemStatusStub() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold mb-2">System Status</h1>
      <p className="text-gray-500">Coming in Day 9.</p>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/chain" replace />} />
        <Route path="/chain" element={<ChainExplorer />} />
        <Route path="/policy" element={<PolicyDashboardStub />} />
        <Route path="/regression" element={<RegressionViewStub />} />
        <Route path="/system" element={<SystemStatusStub />} />
        <Route path="*" element={<Navigate to="/chain" replace />} />
      </Route>
    </Routes>
  )
}
