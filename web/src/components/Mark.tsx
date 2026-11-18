import { useId } from "react";

/** Valxel's mark: a hexagon ribbon, cyan into blue, that opens at the right and folds across its
 * lower half (drawn from the logo, for small sizes; the sign-in page shows the logo itself). */
export function Mark({ size = 26 }: { size?: number }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" className="mark">
      <defs>
        <linearGradient id={`${id}a`} x1="10" y1="6" x2="50" y2="56" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#22EDF2" />
          <stop offset="0.5" stopColor="#27B4F2" />
          <stop offset="1" stopColor="#2B7BEF" />
        </linearGradient>
        <linearGradient id={`${id}b`} x1="14" y1="44" x2="56" y2="56" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#2A8CF2" />
          <stop offset="1" stopColor="#1E50D8" />
        </linearGradient>
      </defs>
      <path
        fill={`url(#${id}a)`}
        fillRule="evenodd"
        d="M27.2 5.7Q32 3 36.8 5.7L51.2 13.8Q56 16.5 56 22L56 42Q56 47.5 51.2 50.2L36.8 58.3Q32 61 27.2 58.3L12.8 50.2Q8 47.5 8 42L8 22Q8 16.5 12.8 13.8ZM15.5 23.5Q15.5 20.6 18 19.2L30.6 12.4Q32 11.6 33.4 12.4L49 21Q52.6 23 55.2 27.6L55.2 28.6C47 30.6 41 37 33.6 41.6C28.4 44.8 23.4 46 17.4 45C22.8 43.6 24.2 40 22.8 36.8C21.4 33.8 15.5 33.4 15.5 28.8Z"
      />
      <path
        fill={`url(#${id}b)`}
        d="M55.2 28.6C47 30.6 41 37 33.6 41.6C28.4 44.8 23.4 46 17.4 45Q12 46.2 8.3 44.6Q9.2 48.6 12.8 50.2L27.2 58.3Q32 61 36.8 58.3L51.2 50.2Q56 47.5 56 42L56 28.4Z"
      />
      <path fill="#1A3CB0" opacity="0.4" d="M56 34C49.5 37 45 44.5 37 53L51.2 50.2Q56 47.5 56 42Z" />
      <path
        fill="#0F2A80"
        opacity="0.28"
        d="M33.4 12.4L49 21Q52.6 23 55.2 27.6L56 27.2L56 23Q53.5 19.5 49.8 17.6L35.2 10.2Z"
      />
    </svg>
  );
}

/** The VALXEL wordmark: every letter is straight strokes (the A is an open Λ, as in the logo), so
 * it's drawn rather than set in a font, and takes the text colour. */
export function Wordmark({ height = 14 }: { height?: number }) {
  return (
    <svg
      height={height}
      viewBox="-1.5 -1.5 114 23"
      aria-hidden="true"
      className="wordmark"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.7"
      strokeLinejoin="miter"
      strokeLinecap="butt"
    >
      <path d="M0 0L7 20L14 0" />
      <path d="M20 20L27 0L34 20" />
      <path d="M41 0V20H52.5" />
      <path d="M58 0L72 20M72 0L58 20" />
      <path d="M91 0H79V20H91M79 10H89" />
      <path d="M98 0V20H109.5" />
    </svg>
  );
}

/** Mark, wordmark and product name, as in the logo's lockup. */
export function Lockup() {
  return (
    <span className="lockup">
      <Mark size={28} />
      <span className="lockup-words">
        <Wordmark height={13} />
        <span className="lockup-product">Agentic SDLC</span>
      </span>
      <span className="visually-hidden">Valxel Agentic SDLC</span>
    </span>
  );
}
