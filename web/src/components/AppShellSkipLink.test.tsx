import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { AppShell } from './AppShell'

const user = {
  user_id: '11111111-1111-4111-8111-111111111111',
  subject: 'CgRsb25n',
  email: 'long@loom.local',
  display_name: 'Long',
  groups: ['data-eng'],
}

function renderShell() {
  return render(
    <MemoryRouter>
      <AppShell user={user} onLogout={() => {}} sidebar={null}>
        <button type="button">content button</button>
      </AppShell>
    </MemoryRouter>,
  )
}

describe('skip link', () => {
  it('is the first thing Tab reaches', async () => {
    const events = userEvent.setup()
    renderShell()

    await events.tab()

    // Đứng đầu vòng Tab mới có tác dụng. Đặt nó sau header thì người dùng đã phải
    // Tab qua đúng những thứ nó tồn tại để bỏ qua.
    expect(screen.getByRole('link', { name: /skip to main content/i })).toHaveFocus()
  })

  it('points at a target that can actually take focus', () => {
    renderShell()
    const link = screen.getByRole('link', { name: /skip to main content/i })
    const main = screen.getByRole('main')

    expect(link).toHaveAttribute('href', '#main')
    expect(main).toHaveAttribute('id', 'main')
    // `tabIndex=-1` là bắt buộc. Không có nó, trình duyệt CUỘN tới #main nhưng
    // không đặt tiêu điểm vào đó, nên lần Tab kế tiếp quay về đầu tài liệu — link
    // trông như chạy được trong khi không giải quyết gì cho người dùng bàn phím.
    expect(main).toHaveAttribute('tabindex', '-1')
  })

  it('stays out of the layout until it is focused', () => {
    renderShell()
    // `sr-only` giữ nó khỏi dòng chảy bố cục. Bố cục header là thứ luật của chủ dự
    // án cấm đụng, nên một skip link hiện thường trực sẽ vi phạm ràng buộc đó.
    expect(screen.getByRole('link', { name: /skip to main content/i })).toHaveClass('sr-only')
  })
})
