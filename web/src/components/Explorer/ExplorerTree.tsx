import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import type { TreeNode } from '../../lib/folderTree'

const ITEM_GLYPH: Record<string, string> = {
  lakehouse: '🏛',
  connection: '🔌',
  pipeline: '🔗',
  sql_script: '📝',
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
          // `?folder=/a/b/` hiện ra một cây đóng kín và người dùng phải tự bấm mở
          // lại đúng đường vừa được gửi cho họ.
          defaultOpen={activeFolder ? activeFolder.startsWith(folder.path) : false}
        />
      ))}
      {node.items.map((item) => (
        <li key={item.id} className="py-0.5">
          <Link
            to={`/workspaces/${workspaceId}/items/${item.id}`}
            className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-muted"
          >
            <span aria-hidden>{ITEM_GLYPH[item.type] ?? '📄'}</span>
            {item.display_name}
          </Link>
        </li>
      ))}
    </ul>
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
        // `aria-expanded` chứ không chỉ mũi tên: mũi tên là ▸/▾, và screen reader
        // không đọc được hình dạng ký tự thành trạng thái đóng/mở.
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
