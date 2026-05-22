// Route-level code splitting: each page is in its own chunk, so the
// initial JS payload stays small and the heavy recharts dependency only
// downloads when the user actually visits /regression. Day 9's regression
// page would otherwise push the initial bundle past the 100KB-gzip
// budget per the harvest plan.

import { Routes, Route, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import Layout from './components/Layout.jsx'

const ChainExplorer = lazy(() => import('./pages/ChainExplorer.jsx'))
const PolicyDashboard = lazy(() => import('./pages/PolicyDashboard.jsx'))
const RegressionView = lazy(() => import('./pages/RegressionView.jsx'))
const SystemStatus = lazy(() => import('./pages/SystemStatus.jsx'))

function PageFallback() {
  return (
    <div className="p-8 text-sm text-gray-500">
      Loading…
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/chain" replace />} />
        <Route
          path="/chain"
          element={
            <Suspense fallback={<PageFallback />}>
              <ChainExplorer />
            </Suspense>
          }
        />
        <Route
          path="/policy"
          element={
            <Suspense fallback={<PageFallback />}>
              <PolicyDashboard />
            </Suspense>
          }
        />
        <Route
          path="/regression"
          element={
            <Suspense fallback={<PageFallback />}>
              <RegressionView />
            </Suspense>
          }
        />
        <Route
          path="/system"
          element={
            <Suspense fallback={<PageFallback />}>
              <SystemStatus />
            </Suspense>
          }
        />
        <Route path="*" element={<Navigate to="/chain" replace />} />
      </Route>
    </Routes>
  )
}
