import { useState } from 'react'
import { useParams } from 'react-router'

import { ItemTypeIcon } from '../components/ItemTypeIcon'
import { PageHeader, ToolbarButton } from '../components/PageHeader'

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
    <>
      <PageHeader
        crumbs={[{ label: 'Workspaces', to: '/' }, { label: 'Connections' }]}
        title="Connections"
        actions={
          <ToolbarButton variant="primary" onClick={() => setAdding((v) => !v)}>
            Add connection
          </ToolbarButton>
        }
      />

      <div className="p-5">

      {isPending && (
        <div data-testid="connections-skeleton" className="space-y-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-14 animate-pulse rounded-md border border-line bg-surface" />
          ))}
        </div>
      )}

      {error && (
        <div role="alert" className="rounded-md border border-line bg-surface p-6 text-[13px]">
          {error.message}
        </div>
      )}

      {data && items.length === 0 && !adding && (
        <div className="rounded-md border border-dashed border-line-strong bg-surface p-12 text-center">
          <p className="text-[14px] font-medium">No connections in this workspace</p>
          <p className="mx-auto mt-1.5 max-w-sm text-[13px] text-dim">
            Add one so pipelines and SQL scripts have a data source to point at.
          </p>
        </div>
      )}

      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((item) => {
            const def = definitionOf(item)
            return (
              <li key={item.id} className="rounded-md border border-line bg-surface p-3.5 text-[13px]">
                <div className="flex items-center gap-3">
                  <ItemTypeIcon type="connection" />
                  <span className="font-medium">{item.display_name}</span>
                  <span className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-dim">{def.kind}</span>
                  <div className="flex-1" />
                  <span className="tabular text-[12px] text-dim">
                    {def.host}
                    {def.port ? `:${def.port}` : ''}
                    {def.database ? `/${def.database}` : ''}
                  </span>
                </div>
                {/* Hiện NGUYÊN VĂN. Đây là một đường dẫn, không phải bí mật; che nó bằng
                    dấu sao là nói ngược lại kiến trúc, và người vận hành cần đọc được
                    đúng đường dẫn để biết secret nằm ở đâu. */}
                <p className="mt-2 font-mono text-[12px] text-faint">{def.secret_ref}</p>
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
          className="mt-5 max-w-lg space-y-3 rounded-md border border-line bg-surface p-4"
        >
          <h2 className="text-[14px] font-semibold">New connection</h2>

          <label className="block text-[12px] font-medium text-dim">
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              pattern="[a-z0-9][a-z0-9-]*"
              required
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
            />
            {fieldErrors.name && (
              <span role="alert" className="mt-1 block text-[12px] font-normal text-danger">
                {fieldErrors.name}
              </span>
            )}
          </label>

          <label className="block text-[12px] font-medium text-dim">
            Source
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>

          <label className="block text-[12px] font-medium text-dim">
            Host
            <input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              required
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
            />
            {fieldErrors.host && (
              <span role="alert" className="mt-1 block text-[12px] font-normal text-danger">
                {fieldErrors.host}
              </span>
            )}
          </label>

          <label className="block text-[12px] font-medium text-dim">
            Port
            <input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(e.target.value)}
              required
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
            />
          </label>

          <label className="block text-[12px] font-medium text-dim">
            Database
            <input
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
            />
          </label>

          <label className="block text-[12px] font-medium text-dim">
            Secret reference
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
              className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 font-mono text-[13px] font-normal text-ink"
            />
            {fieldErrors.secret_ref && (
              <span role="alert" className="mt-1 block text-[12px] font-normal text-danger">
                {fieldErrors.secret_ref}
              </span>
            )}
          </label>

          <p className="rounded border border-line bg-raised px-3 py-2 text-[12px] leading-relaxed text-dim">
            Loom stores no passwords. The field above is a path to where the credential lives
            (Vault or a Kubernetes Secret); only the pod running a task can read the real value.
          </p>

          {generalError && (
            <p role="alert" className="text-[13px] text-danger">
              {generalError}
            </p>
          )}

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="h-7 rounded px-3 text-[13px] text-dim hover:bg-hover"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending}
              className="h-7 rounded border border-accent bg-accent px-3 text-[13px] font-medium text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {create.isPending ? 'Creating…' : 'Create'}
            </button>
          </div>
        </form>
      )}
      </div>
    </>
  )
}
