/** Results of actions taken outside a dialog. Errors stay until dismissed; they are written to be read. */
import { X } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";
import { ApiError } from "../api";
import { IconButton, Notice, type Tone } from "./ui";

interface Toast {
  id: number;
  tone: Tone;
  title: string;
  detail?: string;
}

interface ToastApi {
  success: (title: string, detail?: string) => void;
  warn: (title: string, detail?: string) => void;
  error: (title: string, error: unknown) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((list) => list.filter((toast) => toast.id !== id)), []);
  const push = useCallback(
    (tone: Tone, title: string, detail?: string) => {
      const id = nextId.current++;
      setToasts((list) => [...list.slice(-4), { id, tone, title, detail }]);
      if (tone === "ok") setTimeout(() => dismiss(id), 5000);
    },
    [dismiss],
  );

  const value = useMemo<ToastApi>(
    () => ({
      success: (title, detail) => push("ok", title, detail),
      warn: (title, detail) => push("warn", title, detail),
      error: (title, error) =>
        push("bad", title, error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error)),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((toast) => (
          <Notice
            key={toast.id}
            tone={toast.tone}
            title={toast.title}
            actions={
              <IconButton label="Dismiss" onClick={() => dismiss(toast.id)}>
                <X className="icon" aria-hidden />
              </IconButton>
            }
          >
            {toast.detail && <span className="error-detail">{toast.detail}</span>}
          </Notice>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast needs a ToastProvider");
  return context;
}
