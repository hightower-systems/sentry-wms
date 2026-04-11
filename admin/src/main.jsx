import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from './auth.jsx'
import { WarehouseProvider } from './warehouse.jsx'
import App from './App.jsx'
import './App.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <WarehouseProvider>
          <App />
        </WarehouseProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>
)
