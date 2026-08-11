import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useModalFocus } from './useModalFocus'

function Modal({ onClose }: { onClose: () => void }) {
  const ref = useModalFocus<HTMLDivElement>(onClose)
  return (
    <div ref={ref} role="dialog" aria-modal="true" aria-label="Test">
      <button type="button">first</button>
      <button type="button">second</button>
    </div>
  )
}

function Harness({ onClose = () => {} }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <button type="button">outside</button>
      {open && (
        <Modal
          onClose={() => {
            setOpen(false)
            onClose()
          }}
        />
      )}
    </>
  )
}

describe('useModalFocus', () => {
  it('moves focus into the dialog when it opens', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: 'open' }))

    // Không có bước này, người dùng trình đọc màn hình không hề biết có gì vừa mở:
    // `aria-modal` chỉ nói với cây a11y rằng phần còn lại bị che, nó KHÔNG di chuyển
    // tiêu điểm.
    expect(screen.getByRole('button', { name: 'first' })).toHaveFocus()
  })

  it('keeps Tab inside the dialog', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('button', { name: 'open' }))

    await user.tab()
    expect(screen.getByRole('button', { name: 'second' })).toHaveFocus()

    // Vòng lại phần tử đầu, KHÔNG đi ra nút "outside" phía sau lớp phủ. `aria-modal`
    // không chặn Tab — chỉ có bẫy tiêu điểm mới chặn.
    await user.tab()
    expect(screen.getByRole('button', { name: 'first' })).toHaveFocus()
  })

  it('wraps backwards on Shift+Tab', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('button', { name: 'open' }))

    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: 'second' })).toHaveFocus()
  })

  it('returns focus to whatever opened it', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const opener = screen.getByRole('button', { name: 'open' })

    await user.click(opener)
    await user.keyboard('{Escape}')

    // Không trả tiêu điểm thì nó rơi về <body>, và lần Tab kế tiếp bắt đầu lại từ
    // đầu tài liệu — người dùng bàn phím mất chỗ đang đứng sau MỖI hộp thoại.
    expect(opener).toHaveFocus()
  })

  it('closes on Escape without needing a click inside first', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: 'open' }))

    await user.keyboard('{Escape}')

    // Đây là lý do hook này phải TỰ xử lý Escape thay vì để `onKeyDown` trên div.
    // React chỉ nhận sự kiện bàn phím nổi lên từ phần tử ĐANG CÓ tiêu điểm; khi
    // tiêu điểm còn nằm ngoài dialog, handler trên div không bao giờ chạy. Ba hộp
    // thoại của Loom đã ở đúng tình trạng đó: có mã bắt Escape, và nó không chạy
    // cho tới khi người dùng bấm chuột vào trong.
    expect(onClose).toHaveBeenCalledOnce()
  })
})
