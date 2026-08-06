import { useState } from 'react'

import { ProblemError } from '../lib/problem'
import { useDomains } from '../lib/useDomains'
import { useCreateWorkspace } from '../lib/useWorkspaces'

/**
 * Tạo workspace.
 *
 * Chỉ mở được khi `tenant_role` là admin — server chặn bằng 404, và hiện hộp thoại cho
 * người không tạo được là mời họ điền xong một form rồi mới biết mình không có quyền.
 */
export function NewWorkspaceDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [domainId, setDomainId] = useState('')
  const create = useCreateWorkspace()
  const domains = useDomains()

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
              ...(domainId ? { domain_id: domainId } : {}),
            },
            { onSuccess: onClose },
          )
        }}
        role="dialog"
        aria-modal="true"
        aria-label="New workspace"
        className="my-auto w-full max-w-lg space-y-3 rounded-lg border border-line-strong bg-surface p-5 shadow-2xl shadow-ink/20"
      >
        <h2 className="text-[15px] font-semibold">New workspace</h2>

        <label className="block text-[12px] font-medium text-dim">
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            // Khớp ĐÚNG regex của backend. Đây là gợi ý sớm, không phải phép kiểm — sai
            // khác giữa hai bên chỉ làm người dùng bị từ chối ở một chỗ mà bên kia cho qua.
            pattern="[a-z0-9][a-z0-9-]*"
            title="lowercase letters, digits and hyphens; cannot start with a hyphen"
            required
            className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
          />
          {/* `name` đi vào `storage_prefix` ở Giai đoạn 2 — nói ra để người dùng biết
              đây không phải một cái nhãn đổi lúc nào cũng được. */}
          <p className="mt-1 text-[11px] font-normal text-faint">
            Technical identifier. It cannot be changed later.
          </p>
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

        <label className="block text-[12px] font-medium text-dim">
          Domain
          <select
            value={domainId}
            onChange={(e) => setDomainId(e.target.value)}
            className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
          >
            <option value="">None</option>
            {(domains.data?.items ?? []).map((d) => (
              <option key={d.id} value={d.id}>
                {d.display_name}
              </option>
            ))}
          </select>
          <p className="mt-1 text-[11px] font-normal text-faint">
            A role granted on a domain applies to every workspace inside it.
          </p>
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
