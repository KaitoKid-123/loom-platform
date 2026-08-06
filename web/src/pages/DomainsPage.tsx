import { useState } from 'react'
import { useSearchParams } from 'react-router'

import { PageHeader, ToolbarButton } from '../components/PageHeader'
import { ProblemError } from '../lib/problem'
import { useCreateDomain, useDomains } from '../lib/useDomains'
import { useWorkspaces } from '../lib/useWorkspaces'

/**
 * Domain — nhóm workspace theo lĩnh vực nghiệp vụ, như Fabric.
 *
 * Ai cũng ĐỌC được danh sách này, cố ý khác workspace: danh sách domain là bản đồ tổ
 * chức, và biết phòng Tài chính tồn tại không phải là đọc được dữ liệu của họ. Chỉ admin
 * cấp tenant mới tạo được, nên nút tạo ẩn với người khác.
 */
export function DomainsPage() {
  const { data, isPending, error } = useDomains()
  const workspaces = useWorkspaces()
  const [searchParams, setSearchParams] = useSearchParams()
  const creating = searchParams.get('new') === '1'

  const setNew = (open: boolean) => {
    const next = new URLSearchParams(searchParams)
    if (open) next.set('new', '1')
    else next.delete('new')
    setSearchParams(next, { replace: !open })
  }

  const canCreate = workspaces.data?.tenant_role === 'admin'
  const items = data?.items ?? []

  return (
    <>
      <PageHeader
        crumbs={[{ label: 'Domains' }]}
        title="Domains"
        actions={
          canCreate ? (
            <ToolbarButton variant="primary" onClick={() => setNew(true)}>
              New domain
            </ToolbarButton>
          ) : null
        }
      />

      <div className="p-5">
        {isPending && (
          <div data-testid="domains-skeleton" className="space-y-2">
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

        {data && items.length === 0 && (
          <div className="rounded-md border border-dashed border-line-strong bg-surface p-12 text-center">
            <p className="text-[14px] font-medium">No domains yet</p>
            <p className="mx-auto mt-1.5 max-w-md text-[13px] text-dim">
              A domain groups workspaces by business area. Granting a role on a domain gives
              it on every workspace inside, instead of repeating the grant on each one.
            </p>
          </div>
        )}

        {items.length > 0 && (
          <div className="overflow-hidden rounded-md border border-line bg-surface">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-line-strong bg-raised text-left">
                  <th
                    scope="col"
                    className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-dim"
                  >
                    Name
                  </th>
                  <th
                    scope="col"
                    className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-dim"
                  >
                    Workspaces
                  </th>
                  <th
                    scope="col"
                    className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-dim"
                  >
                    Your role
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((d) => (
                  <tr key={d.id} className="border-b border-line last:border-0 hover:bg-hover">
                    <td className="px-3 py-1.5">
                      <span className="font-medium">{d.display_name}</span>
                      <span className="ml-2 font-mono text-[11px] text-faint">{d.name}</span>
                      {d.description && (
                        <p className="text-[12px] text-dim">{d.description}</p>
                      )}
                    </td>
                    <td className="tabular px-3 py-1.5 text-dim">{d.workspace_count}</td>
                    <td className="px-3 py-1.5 text-dim">
                      {/* Dấu gạch chứ không ô trống: "không có vai trò" là một câu trả
                          lời, còn một ô trống trông như dữ liệu chưa tải xong. */}
                      {d.my_role ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {creating && <NewDomainDialog onClose={() => setNew(false)} />}
    </>
  )
}

function NewDomainDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const create = useCreateDomain()

  const fieldErrors =
    create.error instanceof ProblemError ? create.error.fieldErrors : ({} as Record<string, string>)
  const generalError =
    create.error && Object.keys(fieldErrors).length === 0 ? create.error.message : null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/30 p-8"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate(
            {
              name,
              display_name: displayName || name,
              ...(description ? { description } : {}),
            },
            { onSuccess: onClose },
          )
        }}
        role="dialog"
        aria-modal="true"
        aria-label="New domain"
        className="my-auto w-full max-w-lg space-y-3 rounded-lg border border-line-strong bg-surface p-5 shadow-2xl shadow-ink/20"
      >
        <h2 className="text-[15px] font-semibold">New domain</h2>

        <label className="block text-[12px] font-medium text-dim">
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            pattern="[a-z0-9][a-z0-9-]*"
            title="lowercase letters, digits and hyphens; cannot start with a hyphen"
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
          Display name
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="defaults to the name"
            className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
          />
        </label>

        <label className="block text-[12px] font-medium text-dim">
          Description
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
          />
        </label>

        {generalError && (
          <p role="alert" className="text-[13px] text-danger">
            {generalError}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
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
    </div>
  )
}
