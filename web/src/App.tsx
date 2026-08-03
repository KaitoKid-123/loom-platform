import { AppShell } from './components/AppShell'

// Stand-in tối thiểu cho Giai đoạn 0 — chưa có xác thực hay gọi API.
// Task 11 thay thế bằng /api/v1/me thật và chuyển hướng đăng nhập.
const STAND_IN_USER = {
  subject: 'stand-in',
  email: 'stand-in@loom.local',
  display_name: 'Stand-in User',
}

export function App() {
  return <AppShell user={STAND_IN_USER} onLogout={() => {}} />
}

export default App
