import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's class joiner: `clsx` for conditionals, `tailwind-merge` so a
 *  caller's `className` beats a component's own conflicting utility instead of
 *  depending on stylesheet order. The app's own `.btn*` / `.panel` classes ride
 *  along untouched — `twMerge` only rewrites recognised Tailwind utilities. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}