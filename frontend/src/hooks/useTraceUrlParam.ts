import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * 统一从 URL 查询参数读取/写回 `trace_id` 与 `step_id`。
 *
 * 跨页面上下文只依赖 URL（可刷新恢复、可分享），不引入全局状态管理。
 */
export function useTraceUrlParam() {
  const [params, setParams] = useSearchParams()
  const traceId = params.get('trace_id')?.trim() ?? ''
  const stepId = params.get('step_id')?.trim() ?? ''

  const update = useCallback(
    (patch: { trace_id?: string | null; step_id?: string | null }) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [key, value] of Object.entries(patch)) {
            if (value) next.set(key, value)
            else next.delete(key)
          }
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )

  return {
    traceId,
    stepId,
    setTraceId: useCallback((id: string | null) => update({ trace_id: id }), [update]),
    setStepId: useCallback((id: string | null) => update({ step_id: id }), [update]),
  }
}
