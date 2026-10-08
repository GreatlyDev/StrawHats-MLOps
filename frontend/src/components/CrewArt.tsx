export function StrawHatMark({ className = '' }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 80 80"
      fill="none"
      aria-hidden="true"
    >
      <g stroke="currentColor" strokeWidth="4" strokeLinecap="round">
        <path d="m16 53 47 17M17 70l46-17" />
        <path d="m12 50 5 3-4 5M67 66l-4 4 5 3M12 67l5 3-4 4M68 50l-5 3 5 4" />
      </g>
      <path
        d="M22 34c0-13 36-13 36 0v13c0 8-6 13-11 14v8H33v-8c-5-1-11-6-11-14Z"
        fill="#fff7e3"
        stroke="currentColor"
        strokeWidth="3"
      />
      <circle cx="31" cy="43" r="6" fill="currentColor" />
      <circle cx="49" cy="43" r="6" fill="currentColor" />
      <path d="m40 49-3 5h6Z" fill="currentColor" />
      <path
        d="M29 58c7 5 15 5 22 0M36 61v7m8-7v7"
        stroke="currentColor"
        strokeWidth="2"
      />
      <path
        d="M22 28c0-23 36-23 36 0l-2 6H24Z"
        fill="#e5bb56"
        stroke="currentColor"
        strokeWidth="3"
      />
      <path d="M23 26h34v7H23Z" fill="#b8322b" />
      <ellipse
        cx="40"
        cy="34"
        rx="31"
        ry="6"
        fill="#e5bb56"
        stroke="currentColor"
        strokeWidth="3"
      />
    </svg>
  );
}

export function TransponderSnail() {
  return (
    <svg viewBox="0 0 120 100" fill="none" aria-hidden="true">
      <path
        d="M24 82c0-20 10-32 29-32s39 6 48 29l-2 8H31Z"
        fill="#e8be61"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <circle
        cx="49"
        cy="60"
        r="27"
        fill="#f3d697"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <path
        d="M48 80c-16 0-24-16-18-29 5-11 22-15 32-4 7 8 3 21-7 22-7 1-11-7-7-12"
        stroke="#b8322b"
        strokeWidth="4"
        strokeLinecap="round"
      />
      <path
        d="M79 78c-5-13-4-27 4-31 7-4 18 3 16 18l-2 17"
        fill="#eee3bb"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <path d="m82 49-4-15m15 17 7-15" stroke="#183d4b" strokeWidth="3" />
      <circle
        cx="77"
        cy="32"
        r="7"
        fill="#fff8e8"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <circle
        cx="102"
        cy="33"
        r="7"
        fill="#fff8e8"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <circle cx="78" cy="33" r="2.5" fill="#183d4b" />
      <circle cx="101" cy="34" r="2.5" fill="#183d4b" />
      <path
        d="M83 67c3 3 6 3 9 0"
        stroke="#183d4b"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M24 34c-4-2-6-13-1-16 11-7 25-6 36 1 4 3 1 13-4 14l-8-6-16-1Z"
        fill="#b8322b"
        stroke="#183d4b"
        strokeWidth="3"
      />
      <path d="M39 29v7" stroke="#183d4b" strokeWidth="3" />
    </svg>
  );
}
