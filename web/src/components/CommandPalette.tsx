import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { useSearch } from '../lib/useSearch'
import { OPEN_PALETTE_EVENT } from './AppShell'

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
    // Nút tìm kiếm trên header mở bảng lệnh qua một window event, không qua context:
    // bảng lệnh nằm trong `AppLayout` còn nút nằm trong `AppShell`, và một context chỉ
    // để nối hai thứ đó là ba file nữa cho một mũi tên một chiều.
    const onOpen = () => setOpen(true)
    window.addEventListener('keydown', onKey)
    window.addEventListener(OPEN_PALETTE_EVENT, onOpen)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener(OPEN_PALETTE_EVENT, onOpen)
    }
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
      { id: 'go:workspaces', label: 'Go to workspaces', run: () => navigate('/') },
    ]
    // Hành động lọc theo CÙNG chuỗi tìm kiếm, để palette không đẩy một hành động không
    // liên quan lên trên kết quả người dùng đang tìm.
    const q = term.trim().toLowerCase()
    return [...hits, ...actions.filter((a) => !q || a.label.toLowerCase().includes(q))]
  }, [data, term, navigate])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-ink/25 pt-[12vh] backdrop-blur-[1px]"
      onClick={() => setOpen(false)}
    >
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        // Chặn nổi bọt: bấm bên TRONG bảng không được đóng nó, còn bấm ra ngoài thì có.
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-lg border border-line-strong bg-surface shadow-2xl shadow-ink/20"
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
          placeholder="Search items or run a command…"
          aria-label="Search items or run a command"
          className="w-full border-b border-line bg-transparent px-4 py-3 text-[14px] outline-none placeholder:text-faint"
        />
        <ul role="listbox" aria-label="Results" className="max-h-[52vh] overflow-auto py-1">
          {commands.length === 0 && (
            <li className="px-4 py-6 text-center text-[13px] text-dim">
              {/* "đang tìm" và "không có gì" là HAI chuyện, và gộp lại làm người dùng
                  kết luận item của mình không tồn tại ngay khi request còn đang bay.
                  Lỗi lại là chuyện thứ ba: "Không có kết quả" khi server đang lỗi là
                  một câu sai.

                  KHÔNG có nhánh "nhập để tìm": lúc chuỗi rỗng thì danh sách hành động
                  vẫn hiện, nên nhánh đó không bao giờ tới được — và mở bảng ra thấy sẵn
                  các lệnh hữu ích hơn một câu bảo người dùng gõ. */}
              {error ? error.message : isFetching ? 'Searching…' : 'No results'}
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
                className={`flex w-full items-center gap-3 px-4 py-2 text-left text-[13px] ${
                  index === cursor ? 'bg-selected' : ''
                }`}
              >
                <span className="truncate font-medium">{command.label}</span>
                {command.hint && (
                  <span className="ml-auto shrink-0 font-mono text-[11px] text-faint">{command.hint}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
