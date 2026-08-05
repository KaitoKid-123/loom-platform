import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import type { TreeItem, TreeNode } from '../../lib/folderTree'
import { describeError, useDeleteItem } from '../../lib/useItemMutations'
import { RenameInline } from './RenameInline'

const ITEM_GLYPH: Record<string, string> = {
  lakehouse: '🏛',
  connection: '🔌',
  pipeline: '🔗',
  sql_script: '📝',
}

/**
 * ETag của một item dựng từ `version` — đó ĐÚNG là quy ước của backend
 * (`_etag()` trong `routers/items.py` trả `W/"{version}"`), và `version` đã nằm trong
 * phản hồi danh sách. Nên đổi tên không cần một GET phụ để lấy ETag.
 */
function etagOf(item: TreeItem): string {
  return `W/"${item.version}"`
}

interface Props {
  node: TreeNode
  workspaceId: string
  depth?: number
}

export function ExplorerTree({ node, workspaceId, depth = 0 }: Props) {
  const [searchParams] = useSearchParams()
  const activeFolder = searchParams.get('folder')

  return (
    <ul className={depth === 0 ? '' : 'ml-4 border-l border-line pl-2'}>
      {node.folders.map((folder) => (
        <FolderRow
          key={folder.path}
          folder={folder}
          workspaceId={workspaceId}
          depth={depth}
          // Mở sẵn nhánh chứa folder đang chọn. Không có nó thì deep-link vào
          // `?folder=/a/b/` hiện ra một cây đóng kín và người dùng phải tự bấm mở lại
          // đúng đường vừa được gửi cho họ.
          defaultOpen={activeFolder ? activeFolder.startsWith(folder.path) : false}
        />
      ))}
      {node.items.map((item) => (
        <ItemRow key={item.id} item={item} workspaceId={workspaceId} />
      ))}
    </ul>
  )
}

function ItemRow({ item, workspaceId }: { item: TreeItem; workspaceId: string }) {
  const [renaming, setRenaming] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const remove = useDeleteItem(workspaceId)

  if (renaming) {
    return (
      <li className="py-0.5">
        <RenameInline
          workspaceId={workspaceId}
          itemId={item.id}
          etag={etagOf(item)}
          current={item.display_name}
          onDone={() => setRenaming(false)}
        />
      </li>
    )
  }

  return (
    <li className="group py-0.5">
      <div className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-muted">
        <span aria-hidden>{ITEM_GLYPH[item.type] ?? '📄'}</span>
        <Link to={`/workspaces/${workspaceId}/items/${item.id}`} className="min-w-0 flex-1 truncate">
          {item.display_name}
        </Link>

        {/* Hiện khi hover HOẶC khi lấy nét — `group-hover` một mình làm hai nút này
            không tồn tại với người dùng bàn phím. */}
        <div className="flex gap-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100">
          <button
            type="button"
            onClick={() => setRenaming(true)}
            className="rounded px-2 py-0.5 text-xs text-dim hover:bg-surface"
          >
            Đổi tên
          </button>
          {confirmingDelete ? (
            // Hai bước thay vì `window.confirm`: xoá mềm phục hồi được, nhưng một cú
            // bấm lạc tay vẫn làm item biến khỏi cây của cả nhóm.
            <>
              <button
                type="button"
                onClick={() => remove.mutate(item.id)}
                disabled={remove.isPending}
                className="rounded px-2 py-0.5 text-xs text-red-400 hover:bg-surface disabled:opacity-50"
              >
                Xác nhận xoá
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(false)}
                className="rounded px-2 py-0.5 text-xs text-dim hover:bg-surface"
              >
                Không
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className="rounded px-2 py-0.5 text-xs text-dim hover:bg-surface"
            >
              Xoá
            </button>
          )}
        </div>
      </div>
      {remove.isError && remove.error && (
        <p role="alert" className="px-2 text-xs text-dim">
          {describeError(remove.error)}
        </p>
      )}
    </li>
  )
}

function FolderRow({
  folder,
  workspaceId,
  depth,
  defaultOpen,
}: {
  folder: TreeNode
  workspaceId: string
  depth: number
  defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        // `aria-expanded` chứ không chỉ mũi tên: mũi tên là ▸/▾, và screen reader không
        // đọc được hình dạng ký tự thành trạng thái đóng/mở.
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm hover:bg-muted"
      >
        <span aria-hidden>{open ? '▾' : '▸'}</span>
        {folder.name}
      </button>
      {open && <ExplorerTree node={folder} workspaceId={workspaceId} depth={depth + 1} />}
    </li>
  )
}
