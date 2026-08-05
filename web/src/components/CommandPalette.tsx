import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { useSearch } from '../lib/useSearch'

interface Command {
  id: string
  label: string
  hint?: string
  run: () => void
}

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [cursor, setCursor] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  const { data, isFetching, error } = useSearch(open ? term : '')

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // `metaKey` HOẶC `ctrlKey`: ⌘K trên macOS, Ctrl+K ở nơi khác. Thiếu `metaKey`
      // thì mọi người dùng macOS bấm đúng tổ hợp mà spec quảng cáo là cách điều hướng
      // chính, và không có gì xảy ra.
      if (event.key === 'k' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        setOpen((v) => !v)
      }
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (open) {
      inputRef.current?.focus()
    } else {
      // Dọn khi đóng: mở lại phải là một bảng trắng. Giữ chuỗi cũ làm người dùng thấy
      // kết quả của lần tìm trước và tưởng đó là kết quả của lần này.
      setTerm('')
      setCursor(0)
    }
  }, [open])

  const commands = useMemo<Command[]>(() => {
    const hits: Command[] = (data?.items ?? []).map((hit) => ({
      id: `item:${hit.id}`,
      label: hit.display_name,
      hint: `${hit.type} · ${hit.folder_path}`,
      run: () => navigate(`/workspaces/${hit.workspace_id}/items/${hit.id}`),
    }))
    const actions: Command[] = [
      { id: 'go:workspaces', label: 'Đi tới danh sách workspace', run: () => navigate('/') },
    ]
    // Hành động lọc theo CÙNG chuỗi tìm kiếm, để palette không đẩy một hành động không
    // liên quan lên trên kết quả người dùng đang tìm.
    const q = term.trim().toLowerCase()
    return [...hits, ...actions.filter((a) => !q || a.label.toLowerCase().includes(q))]
  }, [data, term, navigate])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 pt-24"
      onClick={() => setOpen(false)}
    >
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Bảng lệnh"
        // Chặn nổi bọt: bấm bên TRONG bảng không được đóng nó, còn bấm ra ngoài thì có.
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-lg border border-line bg-surface shadow-xl"
      >
        <input
          ref={inputRef}
          value={term}
          onChange={(e) => {
            setTerm(e.target.value)
            // Con trỏ về đầu mỗi khi chuỗi đổi: giữ nó ở vị trí cũ thì Enter chạy một
            // lệnh khác với thứ người dùng đang nhìn.
            setCursor(0)
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setCursor((c) => Math.min(c + 1, commands.length - 1))
            }
            if (e.key === 'ArrowUp') {
              e.preventDefault()
              setCursor((c) => Math.max(c - 1, 0))
            }
            if (e.key === 'Enter' && commands[cursor]) {
              commands[cursor].run()
              setOpen(false)
            }
          }}
          placeholder="Tìm item hoặc chạy lệnh…"
          aria-label="Tìm item hoặc chạy lệnh"
          className="w-full border-b border-line bg-transparent px-4 py-3 outline-none"
        />
        <ul role="listbox" aria-label="Kết quả" className="max-h-80 overflow-auto py-1">
          {commands.length === 0 && (
            <li className="px-4 py-3 text-sm text-dim">
              {/* "đang tìm" và "không có gì" là HAI chuyện, và gộp lại làm người dùng
                  kết luận item của mình không tồn tại ngay khi request còn đang bay.
                  Lỗi lại là chuyện thứ ba: "Không có kết quả" khi server đang lỗi là
                  một câu sai.

                  KHÔNG có nhánh "nhập để tìm": lúc chuỗi rỗng thì danh sách hành động
                  vẫn hiện, nên nhánh đó không bao giờ tới được — và mở bảng ra thấy sẵn
                  các lệnh hữu ích hơn một câu bảo người dùng gõ. */}
              {error ? error.message : isFetching ? 'Đang tìm…' : 'Không có kết quả'}
            </li>
          )}
          {commands.map((command, index) => (
            <li key={command.id}>
              <button
                type="button"
                role="option"
                aria-selected={index === cursor}
                onMouseEnter={() => setCursor(index)}
                onClick={() => {
                  command.run()
                  setOpen(false)
                }}
                className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm ${
                  index === cursor ? 'bg-muted' : ''
                }`}
              >
                <span className="truncate">{command.label}</span>
                {command.hint && (
                  <span className="ml-auto shrink-0 text-xs text-dim">{command.hint}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
