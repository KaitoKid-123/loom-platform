import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SqlEditor } from './SqlEditor'

// `vi.hoisted`: các biến này phải sẵn sàng TRƯỚC KHI `vi.mock` bên dưới chạy — `vi.mock`
// được hoist lên đầu file, nên một `const` khai báo bình thường ở đây sẽ còn ở vùng chết
// tạm thời (TDZ) lúc factory thực thi. Đã kiểm bằng cách bỏ `vi.hoisted` và thấy lỗi
// "Cannot access … before initialization" đúng như vitest cảnh báo.
const { createSpy, disposeSpy, setModelMarkersSpy, registerCompletionItemProviderSpy } = vi.hoisted(
  () => {
    const disposeSpy = vi.fn()
    const setModelMarkersSpy = vi.fn()
    const registerCompletionItemProviderSpy = vi.fn()
    // Một đối tượng CỐ ĐỊNH cho `getModel()` — dùng làm khoá `WeakMap` của
    // `completionRegistry` trong `SqlEditor.tsx`; danh tính đối tượng (`===`) phải ổn
    // định qua nhiều lần render của MỘT instance, nên `getWordUntilPosition` nằm NGAY
    // trên nó thay vì bị bài kiểm spread ra một bản sao (bản sao là một object KHÁC,
    // và `completionRegistry.get` trên nó sẽ luôn trả `undefined`).
    const fakeModel = { getWordUntilPosition: () => ({ startColumn: 1, endColumn: 1 }) }
    const createSpy = vi.fn((_container: HTMLElement, options: Record<string, unknown>) => {
      let value = String(options.value ?? '')
      const listeners: Array<() => void> = []
      return {
        getValue: () => value,
        setValue: (next: string) => {
          value = next
        },
        getModel: () => fakeModel,
        onDidChangeModelContent: (cb: () => void) => {
          listeners.push(cb)
          return { dispose: vi.fn() }
        },
        dispose: disposeSpy,
        _fireChange: () => listeners.forEach((l) => l()),
      }
    })
    return { createSpy, disposeSpy, setModelMarkersSpy, registerCompletionItemProviderSpy }
  },
)

vi.mock('monaco-editor', () => ({
  editor: { create: createSpy, setModelMarkers: setModelMarkersSpy },
  languages: {
    registerCompletionItemProvider: registerCompletionItemProviderSpy,
    CompletionItemKind: { Struct: 6, Field: 4 },
  },
  MarkerSeverity: { Error: 8 },
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
  setModelMarkersSpy.mockClear()
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

describe('SqlEditor — gạch đỏ lỗi cú pháp (Giai đoạn 2c Phần A)', () => {
  it('gạch ĐÚNG dòng và cột server báo, không phải hằng số dòng 1', () => {
    // Chứng minh đỏ 3 của Phần A: một cài đặt gạch cứng ở dòng 1 phải làm bài này ĐỎ.
    render(
      <SqlEditor
        value={'select 1\nselect 2\nselect * form orders'}
        markers={[{ line: 3, column: 10, message: "expected FROM, got 'form'" }]}
      />,
    )
    expect(setModelMarkersSpy).toHaveBeenCalledTimes(1)
    const [, , markers] = setModelMarkersSpy.mock.calls[0]!
    expect(markers).toEqual([
      expect.objectContaining({
        startLineNumber: 3,
        startColumn: 10,
        message: "expected FROM, got 'form'",
      }),
    ])
  })

  it('mảng rỗng xoá sạch marker cũ', () => {
    const { rerender } = render(
      <SqlEditor value="select 1" markers={[{ line: 1, column: 1, message: 'x' }]} />,
    )
    setModelMarkersSpy.mockClear()
    rerender(<SqlEditor value="select 1" markers={[]} />)
    const [, , markers] = setModelMarkersSpy.mock.calls.at(-1)!
    expect(markers).toEqual([])
  })
})

describe('SqlEditor — gợi ý bảng/cột (Giai đoạn 2c Phần C)', () => {
  it('đăng ký provider ĐÚNG MỘT LẦN cho ngôn ngữ sql, dù mount nhiều instance', () => {
    // Đăng ký lại mỗi lần mount sẽ gộp gợi ý N lần — xem docstring `SqlEditor.tsx`.
    // `ensureCompletionProviderRegistered` chạy ở SCOPE MODULE nên module đã import
    // (và đăng ký) trước khi bài kiểm này tới lượt chạy; khẳng định nó chỉ chạy một
    // lần trong suốt vòng đời module, không lặp lại mỗi lần `render`.
    const callsBefore = registerCompletionItemProviderSpy.mock.calls.length
    render(<SqlEditor value="select 1" />)
    render(<SqlEditor value="select 2" />)
    expect(registerCompletionItemProviderSpy.mock.calls.length).toBe(callsBefore)
    expect(registerCompletionItemProviderSpy.mock.calls[0]![0]).toBe('sql')
  })

  it('provider tra ĐÚNG gợi ý của model đang được hỏi', () => {
    render(
      <SqlEditor
        value="select "
        completions={[{ label: 'sales.orders', insertText: 'sales.orders', kind: 'table' }]}
      />,
    )
    const provider = registerCompletionItemProviderSpy.mock.calls[0]![1] as {
      provideCompletionItems: (
        model: unknown,
        position: unknown,
      ) => { suggestions: Array<{ label: string }> }
    }
    const model = createSpy.mock.results[0]!.value.getModel()
    const result = provider.provideCompletionItems(model, { lineNumber: 1 })
    expect(result.suggestions.map((s) => s.label)).toEqual(['sales.orders'])
  })
})
