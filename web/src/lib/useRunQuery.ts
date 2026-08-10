import { useEffect, useRef, useState } from 'react'

import {
  type QueryColumn,
  cancelQuery,
  fetchQueryStatus,
  isOverLimitError,
  submitQuery,
} from './queryApi'

export type RunPhase = 'idle' | 'submitting' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface RunResult {
  columns: QueryColumn[]
  rows: unknown[][]
  truncated: boolean
  rowCount: number
}

export interface RunQueryState {
  phase: RunPhase
  queryId: string | null
  result: RunResult | null
  /** Chỉ có giá trị khi `phase === 'failed'` VÀ lỗi tới từ polling (GET trả
   * `status: "failed"`) — thất bại lúc THỰC THI, không phải lúc nộp. */
  error: string | null
  /** `error` có khớp mẫu "vượt giới hạn" (byte quét/thời gian) hay không — xem
   * `isOverLimitError`. */
  overLimit: boolean
  /** Lỗi từ chính `POST` (cú pháp/quyền/khác) — KHÁC `error`: xảy ra trước khi có
   * `queryId` nào, nên không gắn được vào một lượt chạy cụ thể. */
  submitError: Error | null
}

const IDLE_STATE: RunQueryState = {
  phase: 'idle',
  queryId: null,
  result: null,
  error: null,
  overLimit: false,
  submitError: null,
}

const DEFAULT_POLL_INTERVAL_MS = 500

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Vòng đời bất đồng bộ của MỘT query: nộp, poll, huỷ — bốn tình huống bắt buộc của
 * Phần A Giai đoạn 2c:
 *
 *   1. Unmount giữa chừng     -> không rò rỉ vòng poll, không `setState` sau unmount.
 *   2. Bấm chạy hai lần        -> không có hai vòng poll SỐNG cùng lúc.
 *   3. Xong TRƯỚC lần hỏi đầu  -> vẫn hiện kết quả (không có nhánh nào giả định phải
 *      "running" trước khi "succeeded" — mọi trạng thái cuối được xử lý y hệt nhau bất
 *      kể nó về ở lượt poll thứ mấy).
 *   4. Huỷ rồi chạy lại        -> kết quả CŨ không đè lên kết quả MỚI.
 *
 * Cơ chế DUY NHẤT cho cả bốn: một bộ đếm thế hệ (`generationRef`). Mỗi `run()` — và
 * mỗi `cancel()` — tăng nó lên MỘT nấc mới; mọi thao tác bất đồng bộ (await một fetch,
 * một `sleep`) so `myGen` của CHÍNH NÓ với `generationRef.current` NGAY SAU khi tỉnh
 * dậy, trước khi chạm `setState` hay lên lịch bước tiếp theo. Một thao tác thấy lệch
 * thì tự bỏ cuộc êm — không cần cờ `ignore` riêng cho unmount, không cần `AbortController`
 * cho double-run: cùng một con số trả lời được cả ba câu "tôi có còn là lượt chạy được
 * quan tâm không".
 */
export function useRunQuery(pollIntervalMs: number = DEFAULT_POLL_INTERVAL_MS) {
  const [state, setState] = useState<RunQueryState>(IDLE_STATE)
  const generationRef = useRef(0)
  const activeQueryIdRef = useRef<string | null>(null)

  // Unmount: thao tác đang bay (fetch, sleep) sẽ thấy `myGen` lệch ở lần kiểm TIẾP
  // THEO và tự dừng — không có `setState` nào chạm tới sau khi component đã rời cây.
  useEffect(() => {
    return () => {
      generationRef.current += 1
    }
  }, [])

  async function pollLoop(queryId: string, myGen: number): Promise<void> {
    for (;;) {
      if (myGen !== generationRef.current) return

      let status
      try {
        status = await fetchQueryStatus(queryId)
      } catch (err) {
        if (myGen !== generationRef.current) return
        setState({
          phase: 'failed',
          queryId,
          result: null,
          error: err instanceof Error ? err.message : String(err),
          overLimit: false,
          submitError: null,
        })
        return
      }
      if (myGen !== generationRef.current) return

      if (status.status === 'running') {
        await sleep(pollIntervalMs)
        continue
      }

      if (status.status === 'succeeded') {
        const rows = status.rows ?? []
        setState({
          phase: 'succeeded',
          queryId,
          result: {
            columns: status.columns ?? [],
            rows,
            truncated: status.truncated ?? false,
            rowCount: status.row_count ?? rows.length,
          },
          error: null,
          overLimit: false,
          submitError: null,
        })
        return
      }

      if (status.status === 'failed') {
        const message = status.error ?? 'the query failed'
        setState({
          phase: 'failed',
          queryId,
          result: null,
          error: message,
          overLimit: isOverLimitError(message),
          submitError: null,
        })
        return
      }

      // 'cancelled' — DELETE của MỘT phiên khác, hoặc `cancel()` thắng race với
      // `pollLoop` giữa hai lần GET (huỷ không tăng `generationRef` cho tới khi chính
      // `cancelQuery` trả lời — xem `cancel` bên dưới — nên nhánh này vẫn có thể là
      // nơi trạng thái 'cancelled' được ghi nhận đầu tiên).
      setState({
        phase: 'cancelled',
        queryId,
        result: null,
        error: null,
        overLimit: false,
        submitError: null,
      })
      return
    }
  }

  function run(lakehouseId: string, sql: string): void {
    const myGen = (generationRef.current += 1)
    activeQueryIdRef.current = null
    setState({ ...IDLE_STATE, phase: 'submitting' })

    void (async () => {
      let queryId: string
      try {
        const created = await submitQuery(lakehouseId, sql)
        queryId = created.queryId
      } catch (err) {
        if (myGen !== generationRef.current) return
        setState({
          ...IDLE_STATE,
          submitError: err instanceof Error ? err : new Error(String(err)),
        })
        return
      }
      if (myGen !== generationRef.current) return
      activeQueryIdRef.current = queryId
      setState({ ...IDLE_STATE, phase: 'running', queryId })
      void pollLoop(queryId, myGen)
    })()
  }

  function cancel(): void {
    const queryId = activeQueryIdRef.current
    if (!queryId || state.phase !== 'running') return
    // Xoá NGAY, đồng bộ: một cú bấm Cancel thứ hai (trước khi `setState` của cú đầu
    // kịp render lại) đọc `null` ở đây và tự bỏ qua, thay vì gửi thêm một `DELETE`
    // trùng lặp cho cùng một `query_id`.
    activeQueryIdRef.current = null
    // Tăng NGAY, đồng bộ: `pollLoop` đang bay (dù đang `await fetchQueryStatus` hay
    // `await sleep`) sẽ thấy lệch ở lần kiểm tiếp theo và tự dừng, TRƯỚC KHI `DELETE`
    // kịp trả lời — nếu không, một GET đang bay có thể về SAU `cancelQuery` và ghi
    // đè trạng thái 'cancelled' bằng 'running' hoặc 'succeeded' cũ.
    const myGen = (generationRef.current += 1)
    void cancelQuery(queryId).then(
      () => {
        // Bảo vệ NGƯỢC: nếu người dùng bấm "Run" lại trong lúc `DELETE` còn đang bay,
        // `run()` đã tăng `generationRef` một lần nữa — phản hồi trễ của `cancelQuery`
        // này không được phép ghi 'cancelled' đè lên lượt chạy MỚI.
        if (myGen === generationRef.current) {
          setState({ ...IDLE_STATE, phase: 'cancelled', queryId })
        }
      },
      (err: unknown) => {
        if (myGen === generationRef.current) {
          setState({
            ...IDLE_STATE,
            phase: 'failed',
            queryId,
            error: `could not cancel: ${err instanceof Error ? err.message : String(err)}`,
          })
        }
      },
    )
  }

  return { state, run, cancel }
}
