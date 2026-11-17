/** The building blocks every screen uses. */
import type { UseQueryResult } from "@tanstack/react-query";
import { AlertTriangle, Check, CheckCircle2, Copy, Info, Loader2, X, XCircle } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../api";

export type Tone = "ok" | "warn" | "bad" | "neutral" | "info";

// ── Buttons ─────────────────────────────────────────────────────────────────

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "md" | "sm";
  busy?: boolean;
  icon?: ReactNode;
}

export function Button({ variant = "default", size = "md", busy, icon, children, disabled, className, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      className={`btn btn-${variant} btn-${size}${className ? ` ${className}` : ""}`}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Loader2 className="icon spin" aria-hidden /> : icon}
      {children && <span>{children}</span>}
    </button>
  );
}

export function IconButton({ label, children, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button type="button" className="btn btn-ghost btn-icon" aria-label={label} title={label} {...rest}>
      {children}
    </button>
  );
}

// ── Status ──────────────────────────────────────────────────────────────────

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

const NOTICE_ICON: Record<Tone, ReactNode> = {
  ok: <CheckCircle2 className="icon" aria-hidden />,
  warn: <AlertTriangle className="icon" aria-hidden />,
  bad: <XCircle className="icon" aria-hidden />,
  info: <Info className="icon" aria-hidden />,
  neutral: <Info className="icon" aria-hidden />,
};

export function Notice({
  tone = "info",
  title,
  children,
  actions,
}: {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "bad" ? "alert" : "status"}>
      {NOTICE_ICON[tone]}
      <div className="notice-body">
        {title && <div className="notice-title">{title}</div>}
        {children && <div className="notice-text">{children}</div>}
      </div>
      {actions && <div className="notice-actions">{actions}</div>}
    </div>
  );
}

function errorTitle(error: ApiError): string {
  if (error.nginxRejected) return "nginx rejected this change";
  switch (error.status) {
    case 0:
      return "No answer from the orchestrator";
    case 409:
      return "Refused";
    case 429:
      return "No migration slot is free";
    case 502:
    case 503:
      return "A pool could not be asked";
    case 422:
      return "Not valid";
    default:
      return `Request failed (${error.status})`;
  }
}

/** An error of the API, with its explanation exactly as the API wrote it. */
export function ErrorNotice({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  const detail = apiError ? apiError.detail : error instanceof Error ? error.message : String(error);
  return (
    <Notice tone="bad" title={title ?? (apiError ? errorTitle(apiError) : "Something went wrong")}>
      <span className="error-detail">{detail}</span>
    </Notice>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <Loader2 className="icon spin" aria-hidden />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-text">{children}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

/**
 * Loading, error and empty states of a list. While a poll refreshes data that is already on
 * screen nothing changes: the spinner is only for the first load.
 */
export function QueryView<T>({
  query,
  isEmpty,
  empty,
  children,
}: {
  query: UseQueryResult<T, unknown>;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.data === undefined) {
    if (query.isError) return <ErrorNotice error={query.error} title="Could not load this" />;
    return <Spinner />;
  }
  return (
    <>
      {query.isError && <ErrorNotice error={query.error} title="Could not refresh; showing the last known state" />}
      {isEmpty?.(query.data) ? empty : children(query.data)}
    </>
  );
}

export function Progress({ percent, label }: { percent: number | null; label?: string }) {
  return (
    <div
      className={`progress${percent === null ? " progress-unknown" : ""}`}
      role="progressbar"
      aria-label={label ?? "Progress"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent ?? undefined}
    >
      <div className="progress-bar" style={{ width: `${percent ?? 100}%` }} />
    </div>
  );
}

// ── Layout pieces ───────────────────────────────────────────────────────────

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Panel({
  title,
  meta,
  actions,
  children,
  flush,
}: {
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      {(title || actions) && (
        <div className="panel-header">
          <div className="panel-heading">
            {title && <h2>{title}</h2>}
            {meta && <div className="panel-meta">{meta}</div>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? "panel-body panel-flush" : "panel-body"}>{children}</div>
    </section>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="mono">{children}</span>;
}

// ── Code ────────────────────────────────────────────────────────────────────

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <Button
      size="sm"
      icon={copied ? <Check className="icon" aria-hidden /> : <Copy className="icon" aria-hidden />}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => setCopied(true));
      }}
    >
      {copied ? "Copied" : label}
    </Button>
  );
}

export function CodeBlock({ title, code, copy = true }: { title?: ReactNode; code: string; copy?: boolean }) {
  return (
    <div className="code">
      {(title || copy) && (
        <div className="code-header">
          <span className="mono">{title}</span>
          {copy && <CopyButton text={code} />}
        </div>
      )}
      <pre tabIndex={0}>{code}</pre>
    </div>
  );
}

// ── Forms ───────────────────────────────────────────────────────────────────

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string;
  children: (props: { id: string; "aria-invalid": boolean; "aria-describedby": string | undefined }) => ReactNode;
}) {
  const id = useId();
  const describedBy = [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field${error ? " field-invalid" : ""}`}>
      <label htmlFor={id}>{label}</label>
      {children({ id, "aria-invalid": Boolean(error), "aria-describedby": describedBy })}
      {error && (
        <div className="field-error" id={`${id}-error`}>
          {error}
        </div>
      )}
      {hint && (
        <div className="field-hint" id={`${id}-hint`}>
          {hint}
        </div>
      )}
    </div>
  );
}

type TextProps = { label: string; hint?: ReactNode; error?: string; mono?: boolean; onValue: (value: string) => void } & Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "onChange"
>;

export function TextField({ label, hint, error, mono, onValue, ...rest }: TextProps) {
  return (
    <Field label={label} hint={hint} error={error}>
      {(a11y) => (
        <input
          className={mono ? "input mono" : "input"}
          autoComplete="off"
          spellCheck={false}
          {...a11y}
          {...rest}
          onChange={(event) => onValue(event.target.value)}
        />
      )}
    </Field>
  );
}

type SelectProps = { label: string; hint?: ReactNode; error?: string; onValue: (value: string) => void } & Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  "onChange"
>;

export function SelectField({ label, hint, error, onValue, children, ...rest }: SelectProps) {
  return (
    <Field label={label} hint={hint} error={error}>
      {(a11y) => (
        <select className="input" {...a11y} {...rest} onChange={(event) => onValue(event.target.value)}>
          {children}
        </select>
      )}
    </Field>
  );
}

type AreaProps = { label: string; hint?: ReactNode; error?: string; mono?: boolean; onValue: (value: string) => void } & Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  "onChange"
>;

export function TextArea({ label, hint, error, mono, onValue, ...rest }: AreaProps) {
  return (
    <Field label={label} hint={hint} error={error}>
      {(a11y) => (
        <textarea
          className={mono ? "input mono" : "input"}
          spellCheck={false}
          {...a11y}
          {...rest}
          onChange={(event) => onValue(event.target.value)}
        />
      )}
    </Field>
  );
}

export function Checkbox({
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode;
  hint?: ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
      <span>
        {label}
        {hint && <span className="field-hint">{hint}</span>}
      </span>
    </label>
  );
}

export function Switch({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      className="switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-thumb" />
    </button>
  );
}

// ── Dialogs ─────────────────────────────────────────────────────────────────

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]';

export function Modal({
  title,
  onClose,
  children,
  footer,
  size = "md",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    const first = dialog?.querySelector(".modal-body")?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? dialog)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close.current();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const items = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const firstItem = items[0]!;
      const lastItem = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === firstItem) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && document.activeElement === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, []);

  // In the body, not where it was opened: a dialog must not inherit a panel's layout.
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal modal-${size}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref} tabIndex={-1}>
        <div className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <IconButton label="Close" onClick={onClose}>
            <X className="icon" aria-hidden />
          </IconButton>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/**
 * Asks before something that cannot be taken back. With `typeToConfirm` the button stays
 * disabled until that exact text has been typed.
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger = true,
  typeToConfirm,
  busy,
  error,
  onConfirm,
  onClose,
  extra,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  typeToConfirm?: string;
  busy?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onClose: () => void;
  extra?: ReactNode;
}) {
  const [typed, setTyped] = useState("");
  const ready = !typeToConfirm || typed === typeToConfirm;
  return (
    <Modal
      title={title}
      onClose={onClose}
      size="sm"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} busy={busy} disabled={!ready} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="confirm-text">{children}</div>
        {typeToConfirm && (
          <TextField
            label={`Type ${typeToConfirm} to confirm`}
            mono
            value={typed}
            onValue={setTyped}
            onKeyDown={(event) => event.key === "Enter" && ready && onConfirm()}
          />
        )}
        <ErrorNotice error={error} />
        {extra}
      </div>
    </Modal>
  );
}

export function FormDialog({
  title,
  submitLabel,
  busy,
  error,
  onSubmit,
  onClose,
  children,
  size,
  submitDisabled,
  footerExtra,
  afterError,
}: {
  title: string;
  submitLabel: string;
  busy?: boolean;
  error?: unknown;
  onSubmit: () => void;
  onClose: () => void;
  children: ReactNode;
  size?: "sm" | "md" | "lg";
  submitDisabled?: boolean;
  footerExtra?: ReactNode;
  /** Shown under the error, e.g. the separately confirmed override of a refusal. */
  afterError?: ReactNode;
}) {
  const formId = useId();
  return (
    <Modal
      title={title}
      onClose={onClose}
      size={size}
      footer={
        <>
          {footerExtra}
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form={formId} busy={busy} disabled={submitDisabled}>
            {submitLabel}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        className="stack"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        {children}
        <ErrorNotice error={error} />
        {afterError}
      </form>
    </Modal>
  );
}
