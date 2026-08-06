import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { UnauthorizedError, apiGet, apiPostJson } from './api'

export interface Domain {
  id: string
  name: string
  display_name: string
  description: string | null
  /** Số workspace đang thuộc domain — phân biệt "mới tạo" với "vừa bị dọn sạch". */
  workspace_count: number
  /** Vai trò của người gọi ở cấp domain, hoặc `null`: ai cũng ĐỌC được danh sách domain. */
  my_role: string | null
}

export function useDomains() {
  return useQuery<{ items: Domain[] }, Error>({
    queryKey: ['domains'],
    queryFn: () => apiGet('/api/v1/domains'),
    retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 2,
    staleTime: 30_000,
  })
}

export function useCreateDomain() {
  const qc = useQueryClient()
  return useMutation<Domain, Error, { name: string; display_name: string; description?: string }>({
    mutationFn: (body) => apiPostJson<Domain>('/api/v1/domains', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['domains'] })
    },
  })
}
