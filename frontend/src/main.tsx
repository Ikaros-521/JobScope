import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { ToastHost } from './toast'
import { ErrorBoundary } from './ErrorBoundary'
import './styles.css'
import './prepPlan.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary><App /><ToastHost /></ErrorBoundary>
  </React.StrictMode>)
