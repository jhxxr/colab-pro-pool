import { createContext, useCallback, useContext, useState } from 'react'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

export type ToastKind = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  kind: ToastKind
  message: string
}

interface ToastContextValue {
  pushToast: (kind: ToastKind, message: string) => void
}

const ToastContext = createContext<ToastContextValue>({
  pushToast: () => {},
})

let nextToastId = 1

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const removeToast = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const pushToast = useCallback((kind: ToastKind, message: string) => {
    const id = nextToastId++
    setItems((prev) => [...prev.slice(-4), { id, kind, message }])
    setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id))
    }, 4200)
  }, [])

  return (
    <ToastContext.Provider value={{ pushToast }}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
        {items.map((item) => {
          const palette =
            item.kind === 'success'
              ? 'border-success-line bg-success-soft text-success'
              : item.kind === 'error'
                ? 'border-danger-line bg-danger-soft text-danger'
                : 'border-accent-line bg-surface text-ink'
          const Icon =
            item.kind === 'success'
              ? CheckCircle2
              : item.kind === 'error'
                ? AlertCircle
                : Info
          return (
            <div
              key={item.id}
              role={item.kind === 'error' ? 'alert' : 'status'}
              className={`pointer-events-auto flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-sm shadow-floating backdrop-blur ${palette}`}
            >
              <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 flex-1 break-words leading-snug">{item.message}</div>
              <button
                type="button"
                onClick={() => removeToast(item.id)}
                className="-m-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md opacity-70 transition hover:bg-surface/70 hover:opacity-100"
                aria-label="关闭通知"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  return useContext(ToastContext)
}
