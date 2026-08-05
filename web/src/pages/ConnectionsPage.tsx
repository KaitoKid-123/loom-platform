import { useState } from 'react'
import { useParams } from 'react-router'

import type { TreeItem } from '../lib/folderTree'
import { ProblemError } from '../lib/problem'
import { useCreateItem } from '../lib/useItemMutations'
import { useItems } from '../lib/useItems'

const KINDS = ['postgres', 'mysql', 'sqlserver', 'rest'] as const

/**
 * `definition` của một connection. `useItems` trả `TreeItem` không mang `definition`,
 * nên đọc nó qua một kiểu hẹp riêng thay vì nới `TreeItem` — mọi chỗ khác không cần
 * `definition` và nới kiểu ở đó sẽ làm chúng nhận `undefined` mà không ai kiểm.
 */
interface ConnectionDefinition {
  kind?: string
  host?: string
  port?: number
  database?: string | null
  secret_ref?: string
}

function definitionOf(item: TreeItem): ConnectionDefinition {
  return ((item as TreeItem & { definition?: ConnectionDefinition }).definition ??
    {}) as ConnectionDefinition
}

export function ConnectionsPage() {
  const { workspaceId = '' } = useParams()
  const { data, isPending, error } = useItems(workspaceId, 'connection')
  const create = useCreateItem(workspaceId)

  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<(typeof KINDS)[number]>('postgres')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('5432')
  const [database, setDatabase] = useState('')
  const [secretRef, setSecretRef] = useState('')

  // Khoá là phần CUỐI của `loc`, nên `['body','definition','secret_ref']` thành
  // `secret_ref` — xem `ProblemError` ở Task 27.
  const fieldErrors =
    create.error instanceof ProblemError ? create.error.fieldErrors : ({} as Record<string, string>)
  const generalError =
    create.error && Object.keys(fieldErrors).length === 0 ? create.error.message : null

  const items = data?.items ?? []

  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <h1 className="text-lg font-medium">Connections</h1>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setAdding((v) => !v)}
          className="rounded border border-line px-3 py-1 text-sm hover:bg-muted"
        >
          Thêm connection
        </button>
      </div>

      {isPending && (
        <div data-testid="connections-skeleton" className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded bg-muted" />
          ))}
        </div>
      )}

      {error && (
        <div role="alert" className="rounded-lg border border-line p-6 text-sm">
          {error.message}
        </div>
      )}

      {data && items.length === 0 && !adding && (
        <div className="rounded-lg border border-dashed border-line p-8 text-center">
          <p className="text-sm">Workspace này chưa có connection nào.</p>
          <p className="mt-2 text-sm text-dim">
            Thêm một connection để pipeline và SQL script trỏ tới nguồn dữ liệu.
          </p>
        </div>
      )}

      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((item) => {
            const def = definitionOf(item)
            return (
              <li key={item.id} className="rounded-lg border border-line p-4 text-sm">
                <div className="flex items-center gap-3">
                  <span aria-hidden>🔌</span>
                  <span className="font-medium">{item.display_name}</span>
                  <span className="rounded bg-muted px-2 py-0.5 text-xs text-dim">{def.kind}</span>
                  <div className="flex-1" />
                  <span className="text-xs text-dim">
                    {def.host}
                    {def.port ? `:${def.port}` : ''}
                    {def.database ? `/${def.database}` : ''}
                  </span>
                </div>
                {/* Hiện NGUYÊN VĂN. Đây là một đường dẫn, không phải bí mật; che nó bằng
                    dấu sao là nói ngược lại kiến trúc, và người vận hành cần đọc được
                    đúng đường dẫn để biết secret nằm ở đâu. */}
                <p className="mt-2 font-mono text-xs text-dim">{def.secret_ref}</p>
              </li>
            )
          })}
        </ul>
      )}

      {adding && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate(
              {
                type: 'connection',
                name,
                display_name: name,
                definition: {
                  schema_version: 1,
                  kind,
                  host,
                  port: Number(port),
                  ...(database ? { database } : {}),
                  secret_ref: secretRef,
                },
              },
              {
                onSuccess: () => {
                  setAdding(false)
                  setName('')
                  setHost('')
                  setSecretRef('')
                  setDatabase('')
                },
              },
            )
          }}
          className="mt-6 space-y-3 rounded-lg border border-line p-4"
        >
          <h2 className="font-medium">Connection mới</h2>

          <label className="block text-sm">
            Tên
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              pattern="[a-z0-9][a-z0-9-]*"
              required
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1"
            />
            {fieldErrors.name && (
              <span role="alert" className="mt-1 block text-xs">
                {fieldErrors.name}
              </span>
            )}
          </label>

          <label className="block text-sm">
            Loại nguồn
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1"
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-sm">
            Máy chủ
            <input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              required
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1"
            />
            {fieldErrors.host && (
              <span role="alert" className="mt-1 block text-xs">
                {fieldErrors.host}
              </span>
            )}
          </label>

          <label className="block text-sm">
            Cổng
            <input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(e.target.value)}
              required
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1"
            />
          </label>

          <label className="block text-sm">
            Database
            <input
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1"
            />
          </label>

          <label className="block text-sm">
            Tham chiếu secret
            {/* `type="text"`, KHÔNG phải `"password"`. Đây là một ĐƯỜNG DẪN tới nơi chứa
                secret, không phải bản thân secret. Dùng `type=password` là nói với người
                dùng "nhập mật khẩu vào đây", và họ sẽ làm đúng thế — rồi credential nằm
                trong `definition`, đi vào `item_version`, audit và Git ở Giai đoạn 5.
                Backend đã chặn bằng regex, nhưng để người dùng gặp lỗi ở đó thì cái sai
                đã bắt đầu từ chỗ này. */}
            <input
              type="text"
              value={secretRef}
              onChange={(e) => setSecretRef(e.target.value)}
              placeholder="vault://loom/prod/db#password"
              required
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1 font-mono"
            />
            {fieldErrors.secret_ref && (
              <span role="alert" className="mt-1 block text-xs">
                {fieldErrors.secret_ref}
              </span>
            )}
          </label>

          <p className="rounded border border-line bg-muted px-3 py-2 text-xs text-dim">
            Loom không lưu mật khẩu. Ô trên là đường dẫn tới nơi chứa credential (Vault
            hoặc Kubernetes Secret); chỉ pod chạy task đọc được giá trị thật.
          </p>

          {generalError && (
            <p role="alert" className="text-sm">
              {generalError}
            </p>
          )}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="rounded px-3 py-1 text-sm text-dim hover:bg-muted"
            >
              Huỷ
            </button>
            <button
              type="submit"
              disabled={create.isPending}
              className="rounded border border-line px-3 py-1 text-sm hover:bg-muted disabled:opacity-50"
            >
              Tạo
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
