/** A signal head showing caution: the tool's mark, because most of what it does is wait for a person. */
export function Mark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect x="8.5" y="1.5" width="15" height="29" rx="7.5" fill="#0a1410" stroke="#4b5f55" />
      <circle cx="16" cy="8.5" r="3.6" fill="#24332c" />
      <circle cx="16" cy="16" r="3.6" fill="#f2a93b" />
      <circle cx="16" cy="23.5" r="3.6" fill="#24332c" />
    </svg>
  );
}
