import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SqlEditor } from './SqlEditor'

// `vi.hoisted`: các biến này phải sẵn sàng TRƯỚC KHI `vi.mock` bên dưới chạy — `vi.mock`
// được hoist lên đầu file, nên một `const` khai báo bình thường ở đây sẽ còn ở vùng chết
// tạm thời (TDZ) lúc factory thực thi. Đã kiểm bằng cách bỏ `vi.hoisted` và thấy lỗi
// "Cannot access … before initialization" đúng như vitest cảnh báo.
const { createSpy, disposeSpy } = vi.hoisted(() => {
  const disposeSpy = vi.fn()
  const createSpy = vi.fn((_container: HTMLElement, options: Record<string, unknown>) => {
    let value = String(options.value ?? '')
    const listeners: Array<() => void> = []
    return {
      getValue: () => value,
      setValue: (next: string) => {
        value = next
      },
      onDidChangeModelContent: (cb: () => void) => {
        listeners.push(cb)
        return { dispose: vi.fn() }
      },
      dispose: disposeSpy,
      _fireChange: () => listeners.forEach((l) => l()),
    }
  })
  return { createSpy, disposeSpy }
})

vi.mock('monaco-editor', () => ({
  editor: { create: createSpy },
}))

// Đường dẫn KHỚP ĐÚNG với đường thật trong `SqlEditor.tsx` — `?worker` là một transform
// riêng của Vite, và `vi.mock` chặn theo ĐÚNG specifier string, không theo file đích sau
// khi resolve.
vi.mock('monaco-editor/editor/editor.worker?worker', () => ({
  default: class FakeWorker {},
}))

afterEach(() => {
  cleanup()
  createSpy.mockClear()
  disposeSpy.mockClear()
})

describe('SqlEditor', () => {
  it('dựng Monaco với giá trị và ngôn ngữ sql', () => {
    render(<SqlEditor value="select 1" />)
    expect(createSpy).toHaveBeenCalledTimes(1)
    const [, options] = createSpy.mock.calls[0]!
    expect(options.value).toBe('select 1')
    expect(options.language).toBe('sql')
  })

  it('huỷ editor khi component unmount — không rò rỉ instance', () => {
    const { unmount } = render(<SqlEditor value="select 1" />)
    unmount()
    expect(disposeSpy).toHaveBeenCalledTimes(1)
  })

  it('gọi onChange khi nội dung đổi', () => {
    const onChange = vi.fn()
    render(<SqlEditor value="select 1" onChange={onChange} />)
    const instance = createSpy.mock.results[0]!.value as {
      setValue: (v: string) => void
      _fireChange: () => void
    }
    instance.setValue('select 2')
    instance._fireChange()
    expect(onChange).toHaveBeenCalledWith('select 2')
  })

  it('truyền readOnly xuống Monaco khi được yêu cầu', () => {
    render(<SqlEditor value="select 1" readOnly />)
    const [, options] = createSpy.mock.calls[0]!
    expect(options.readOnly).toBe(true)
  })

  it('mặc định KHÔNG readOnly — Monaco ở task này phải gõ được', () => {
    render(<SqlEditor value="select 1" />)
    const [, options] = createSpy.mock.calls[0]!
    expect(options.readOnly).toBeFalsy()
  })
})
