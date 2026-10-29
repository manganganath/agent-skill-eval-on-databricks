export function Loading({ label = "Loading" }: { label?: string }) {
  return <p role="status" aria-live="polite">{label}…</p>;
}
