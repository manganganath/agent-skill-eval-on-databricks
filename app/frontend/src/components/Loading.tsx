export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="loading">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </p>
  );
}
