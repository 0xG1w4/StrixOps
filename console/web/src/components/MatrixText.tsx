import * as React from "react";
import { cn } from "@/lib/utils";

const GLYPHS: Record<string, number[]> = {
  "0": [14, 17, 19, 21, 25, 17, 14],
  "1": [4, 12, 4, 4, 4, 4, 14],
  "2": [14, 17, 1, 2, 4, 8, 31],
  "3": [14, 17, 1, 6, 1, 17, 14],
  "4": [2, 6, 10, 18, 31, 2, 2],
  "5": [31, 16, 16, 30, 1, 17, 14],
  "6": [6, 8, 16, 30, 17, 17, 14],
  "7": [31, 1, 2, 4, 8, 8, 8],
  "8": [14, 17, 17, 14, 17, 17, 14],
  "9": [14, 17, 17, 15, 1, 2, 12],
  A: [14, 17, 17, 31, 17, 17, 17],
  B: [30, 17, 17, 30, 17, 17, 30],
  C: [15, 16, 16, 16, 16, 16, 15],
  D: [30, 17, 17, 17, 17, 17, 30],
  E: [31, 16, 16, 30, 16, 16, 31],
  F: [31, 16, 16, 30, 16, 16, 16],
  G: [14, 17, 16, 23, 17, 17, 14],
  H: [17, 17, 17, 31, 17, 17, 17],
  I: [31, 4, 4, 4, 4, 4, 31],
  J: [7, 2, 2, 2, 2, 18, 12],
  K: [17, 18, 20, 24, 20, 18, 17],
  L: [16, 16, 16, 16, 16, 16, 31],
  M: [17, 27, 21, 21, 17, 17, 17],
  N: [17, 25, 21, 19, 17, 17, 17],
  O: [14, 17, 17, 17, 17, 17, 14],
  P: [30, 17, 17, 30, 16, 16, 16],
  Q: [14, 17, 17, 17, 21, 18, 13],
  R: [30, 17, 17, 30, 20, 18, 17],
  S: [15, 16, 16, 14, 1, 1, 30],
  T: [31, 4, 4, 4, 4, 4, 4],
  U: [17, 17, 17, 17, 17, 17, 14],
  V: [17, 17, 17, 17, 17, 10, 4],
  W: [17, 17, 17, 21, 21, 27, 17],
  X: [17, 17, 10, 4, 10, 17, 17],
  Y: [17, 17, 10, 4, 4, 4, 4],
  Z: [31, 1, 2, 4, 8, 16, 31],
  "-": [0, 0, 0, 31, 0, 0, 0],
  ".": [0, 0, 0, 0, 0, 6, 6],
  ":": [0, 6, 6, 0, 6, 6, 0],
  "/": [1, 2, 2, 4, 8, 8, 16],
  "%": [17, 2, 4, 4, 8, 16, 17],
  "+": [0, 4, 4, 31, 4, 4, 0],
};

export function MatrixText({
  value,
  className,
  label,
}: {
  value: string | number;
  className?: string;
  label?: string;
}) {
  const text = String(value).toUpperCase();
  const characters = [...text];
  const supported = characters.every((character) => character === " " || Boolean(GLYPHS[character]));

  if (!supported) {
    return <span className={className}>{text}</span>;
  }

  const advance = 6;
  const width = Math.max(5, characters.length * advance - 1);

  return (
    <span className={cn("matrix-text", className)} aria-label={label ?? text}>
      <span className="sr-only">{text}</span>
      <svg
        className="matrix-glyph"
        viewBox={`0 0 ${width} 7`}
        preserveAspectRatio="xMinYMid meet"
        aria-hidden="true"
        focusable="false"
      >
        {characters.flatMap((character, characterIndex) => {
          const rows = GLYPHS[character];
          if (!rows) return [];
          return rows.flatMap((rowMask, rowIndex) =>
            Array.from({ length: 5 }, (_, columnIndex) => {
              if ((rowMask & (1 << (4 - columnIndex))) === 0) return null;
              return (
                <circle
                  key={`${characterIndex}-${rowIndex}-${columnIndex}`}
                  cx={characterIndex * advance + columnIndex + 0.5}
                  cy={rowIndex + 0.5}
                  r="0.34"
                  fill="currentColor"
                />
              );
            })
          );
        })}
      </svg>
    </span>
  );
}
