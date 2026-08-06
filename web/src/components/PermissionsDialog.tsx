import { useId, useState } from 'react'

import { describeError } from '../lib/useItemMutations'
import {
  type RoleRow,
  type ScopeType,
  countAdmins,
  useGrantRole,
  useRevokeRole,
  useRoles,
} from '../lib/useRoles'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function principalOf(row: RoleRow) {
  return row.user_id ? { user_id: row.user_id } : { group: row.group ?? '' }
}

interface Props {
  scopeType: ScopeType
  scopeId: string
  onClose: () => void
}

export function PermissionsDialog({ scopeType, scopeId, onClose }: Props) {
  const { data, isPending, error } = useRoles(scopeType, scopeId)
  const grant = useGrantRole(scopeType, scopeId)
  const revoke = useRevokeRole(scopeType, scopeId)
  const [principal, setPrincipal] = useState('')
  const [role, setRole] = useState('')
  const reasonId = useId()

  const rows = data?.items ?? []
  const grantable = data?.grantable_roles ?? []
  const adminCount = countAdmins(rows)

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-ink/30"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Permissions"
        className="w-full max-w-lg rounded-lg border border-line-strong bg-surface p-5 shadow-2xl shadow-ink/20"
      >
        <h2 className="text-[15px] font-semibold">Permissions</h2>

        {isPending && <p className="mt-4 text-[13px] text-dim">Loading…</p>}
        {error && (
          <p role="alert" className="mt-4 text-[13px] text-danger">
            {error.message}
          </p>
        )}

        {data && (
          <>
            {rows.length === 0 && (
              <p className="mt-4 text-[13px] text-dim">Nobody has a role in this scope yet.</p>
            )}
            <ul className="mt-4 space-y-2">
              {rows.map((row) => {
                const isLastAdmin = row.role === 'admin' && adminCount <= 1
                return (
                  <li
                    key={`${row.principal_type}:${row.user_id ?? row.group}`}
                    className="flex items-center gap-2.5 rounded border border-line px-2.5 py-1.5 text-[13px]"
                  >
                    <span aria-hidden>{row.principal_type === 'group' ? '👥' : '👤'}</span>
                    <span className="truncate">{row.group ?? row.user_id}</span>
                    <span className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-dim">{row.role}</span>
                    <div className="flex-1" />
                    <button
                      type="button"
                      disabled={isLastAdmin || revoke.isPending}
                      // `aria-describedby` chứ không chỉ `title`: screen reader đọc được
                      // lý do. Một nút bị vô hiệu mà không nói vì sao còn tệ hơn một nút
                      // bật — người dùng bấm mãi không được và không học được gì.
                      aria-describedby={isLastAdmin ? reasonId : undefined}
                      onClick={() => revoke.mutate(principalOf(row))}
                      className="rounded border border-line-strong px-2 py-0.5 text-[12px] text-dim transition-colors hover:bg-hover hover:text-ink disabled:opacity-40"
                    >
                      Remove
                    </button>
                  </li>
                )
              })}
            </ul>
            {adminCount <= 1 && rows.some((r) => r.role === 'admin') && (
              <p id={reasonId} className="mt-2 text-[12px] text-dim">
                You cannot remove the last admin of this scope — grant another admin first.
              </p>
            )}

            <div className="mt-6 flex items-end gap-2">
              <label className="flex-1 text-[12px] font-medium text-dim">
                User or group
                <input
                  value={principal}
                  onChange={(e) => setPrincipal(e.target.value)}
                  placeholder="group name, or a user UUID"
                  className="mt-1 h-7 w-full rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
                />
              </label>
              <label className="text-[12px] font-medium text-dim">
                Role
                <select
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  className="mt-1 block h-7 rounded border border-line-strong bg-surface px-2 text-[13px] font-normal text-ink"
                >
                  <option value="">—</option>
                  {/* CHỈ những vai trò server cho phép, không một danh sách cứng bốn
                      cái. Hiện `admin` cho một member là mời họ bấm rồi ăn 403. */}
                  {grantable.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={!principal.trim() || !role || grant.isPending}
                onClick={() => {
                  const value = principal.trim()
                  // UUID → người dùng, còn lại → nhóm. Backend đòi ĐÚNG một trong hai và
                  // trả 422 nếu gửi cả hai, nên phải chọn ở đây.
                  grant.mutate(
                    UUID_RE.test(value) ? { role, user_id: value } : { role, group: value },
                    { onSuccess: () => setPrincipal('') },
                  )
                }}
                className="h-7 rounded border border-accent bg-accent px-3 text-[13px] font-medium text-white hover:bg-accent-hover disabled:opacity-40"
              >
                Grant
              </button>
            </div>

            {/* Thông báo của SERVER, nguyên văn: nó nói vai trò nào không gán được vai
                trò nào. Đây cũng là thứ chứng minh lớp chặn thật vẫn hoạt động — hai
                trường dưới đây là lớp thứ hai, không phải chỗ chặn. */}
            {grant.isError && grant.error && (
              <p role="alert" className="mt-2 rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
                {describeError(grant.error)}
              </p>
            )}
            {revoke.isError && revoke.error && (
              <p role="alert" className="mt-2 rounded border border-line bg-danger-soft px-2.5 py-1.5 text-[13px] text-danger">
                {describeError(revoke.error)}
              </p>
            )}
          </>
        )}

        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="h-7 rounded px-3 text-[13px] text-dim hover:bg-hover"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
