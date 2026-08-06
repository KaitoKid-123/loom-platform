import { useState } from 'react'
import { Link } from 'react-router'

import type { TreeItem, TreeNode } from '../../lib/folderTree'
import { describeError, useDeleteItem } from '../../lib/useItemMutations'
import { FolderIcon, ItemTypeIcon, typeLabel } from '../ItemTypeIcon'
import { RenameInline } from './RenameInline'

/**
 * ETag dựng từ `version` — đó ĐÚNG là quy ước của backend (`_etag()` trong
 * `routers/items.py` trả `W/"{version}"`), và `version` đã nằm trong phản hồi danh sách.
 * Nên đổi tên không cần một GET phụ để lấy ETag.
 */
function etagOf(item: TreeItem): string {
  return `W/"${item.version}"`
}

/** "2 hours ago". Ngày tuyệt đối cho mọi thứ quá một tuần — "9 days ago" khó đọc hơn. */
function relativeTime(iso?: string): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const minutes = Math.round((Date.now() - then) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} hr ago`
  const days = Math.round(hours / 24)
  if (days < 7) return `${days} d ago`
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

interface Props {
  node: TreeNode
  workspaceId: string
  /** Điều hướng vào một folder. Cha giữ nó trong URL để deep-link được. */
  onOpenFolder: (path: string) => void
}

export function ItemTable({ node, workspaceId, onOpenFolder }: Props) {
  const empty = node.folders.length === 0 && node.items.length === 0

  return (
    <div className="overflow-hidden rounded-md border border-line bg-surface">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line-strong bg-raised text-left">
            <Th className="w-[46%]">Name</Th>
            <Th className="w-[18%]">Type</Th>
            <Th className="w-[14%]">Version</Th>
            <Th className="w-[14%]">Modified</Th>
            {/* Cột hành động không có tiêu đề chữ, nhưng PHẢI có tên cho screen reader —
                một `th` rỗng đọc thành khoảng lặng và người dùng không biết cột đó là gì. */}
            <th scope="col" className="w-[8%] px-3 py-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {/* Folder LÊN TRƯỚC, như mọi trình duyệt tệp. Trộn lẫn với item thì người dùng
              phải đọc từng dòng mới biết cái nào bấm vào là đi sâu thêm một cấp. */}
          {node.folders.map((folder) => (
            <tr
              key={folder.path}
              className="border-b border-line last:border-0 hover:bg-hover"
            >
              <td className="px-3 py-1.5">
                <button
                  type="button"
                  onClick={() => onOpenFolder(folder.path)}
                  className="flex items-center gap-2 text-left font-medium hover:text-accent hover:underline"
                >
                  <FolderIcon />
                  {folder.name}
                </button>
              </td>
              <td className="px-3 py-1.5 text-dim">Folder</td>
              <td className="px-3 py-1.5 text-dim">—</td>
              <td className="px-3 py-1.5 text-dim">—</td>
              <td className="px-3 py-1.5" />
            </tr>
          ))}

          {node.items.map((item) => (
            <ItemRow key={item.id} item={item} workspaceId={workspaceId} />
          ))}

          {empty && (
            <tr>
              <td colSpan={5} className="px-3 py-10 text-center text-dim">
                This folder is empty.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

function Th({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <th
      scope="col"
      className={`px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-dim ${className}`}
    >
      {children}
    </th>
  )
}

function ItemRow({ item, workspaceId }: { item: TreeItem; workspaceId: string }) {
  const [renaming, setRenaming] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const remove = useDeleteItem(workspaceId)

  if (renaming) {
    return (
      <tr className="border-b border-line last:border-0 bg-selected">
        <td colSpan={5} className="px-3 py-1.5">
          <RenameInline
            workspaceId={workspaceId}
            itemId={item.id}
            etag={etagOf(item)}
            current={item.display_name}
            onDone={() => setRenaming(false)}
          />
        </td>
      </tr>
    )
  }

  return (
    <>
      <tr className="group border-b border-line last:border-0 hover:bg-hover">
        <td className="px-3 py-1.5">
          <Link
            to={`/workspaces/${workspaceId}/items/${item.id}`}
            className="flex items-center gap-2 font-medium hover:text-accent hover:underline"
          >
            <ItemTypeIcon type={item.type} />
            <span className="truncate">{item.display_name}</span>
          </Link>
        </td>
        <td className="px-3 py-1.5 text-dim">{typeLabel(item.type)}</td>
        <td className="px-3 py-1.5 tabular text-dim">v{item.version}</td>
        <td className="px-3 py-1.5 tabular text-dim">{relativeTime(item.updated_at)}</td>
        <td className="px-3 py-1.5">
          {/* Hiện khi hover HOẶC khi lấy nét — `group-hover` một mình làm hai nút này
              không tồn tại với người dùng bàn phím. */}
          <div className="flex justify-end gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
            <RowButton onClick={() => setRenaming(true)}>Rename</RowButton>
            {confirmingDelete ? (
              // Hai bước thay vì `window.confirm`: xoá mềm phục hồi được, nhưng một cú
              // bấm lạc tay vẫn làm item biến khỏi cây của cả nhóm.
              <>
                <RowButton danger disabled={remove.isPending} onClick={() => remove.mutate(item.id)}>
                  Confirm
                </RowButton>
                <RowButton onClick={() => setConfirmingDelete(false)}>Cancel</RowButton>
              </>
            ) : (
              <RowButton onClick={() => setConfirmingDelete(true)}>Delete</RowButton>
            )}
          </div>
        </td>
      </tr>
      {remove.isError && remove.error && (
        <tr>
          <td colSpan={5} className="bg-danger-soft px-3 py-1.5 text-[12px] text-danger">
            <span role="alert">{describeError(remove.error)}</span>
          </td>
        </tr>
      )}
    </>
  )
}

function RowButton({
  danger,
  className = '',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      className={`rounded border border-line-strong bg-surface px-1.5 py-0.5 text-[12px] transition-colors disabled:opacity-45 ${
        danger ? 'text-danger hover:bg-danger-soft' : 'text-dim hover:bg-raised hover:text-ink'
      } ${className}`}
    />
  )
}
