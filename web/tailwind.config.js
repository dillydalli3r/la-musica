/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0a0c",
        panel: "#111114",
        card: "#16161a",
        raise: "#1c1c21",
        border: "#26262c",
        accent: "rgb(var(--accent) / <alpha-value>)",
        "accent-soft": "rgb(var(--accent-soft) / <alpha-value>)",
        // The ink drawn ON an accent fill (the switch thumb, the play button):
        // a real token so `bg-accent-fg` is a utility instead of an arbitrary
        // `[background:rgb(var(--accent-fg))]` at every call site.
        "accent-fg": "rgb(var(--accent-fg) / <alpha-value>)",
      },
      transitionDuration: {
        "motion-fast": "150ms",
        "motion-base": "300ms",
        "motion-slow": "500ms",
      },
      transitionTimingFunction: {
        motion: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
      animation: {
        "page-in": "page-in 0.3s cubic-bezier(0.22, 1, 0.36, 1)",
        "fade-up": "fade-up 0.35s cubic-bezier(0.22, 1, 0.36, 1) backwards",
        pop: "pop-in 0.18s cubic-bezier(0.22, 1, 0.36, 1)",
        fade: "fade-in 0.2s ease",
        "toast-in": "toast-in 0.22s cubic-bezier(0.22, 1, 0.36, 1)",
      },
      keyframes: {
        "page-in": { from: { opacity: "0", transform: "translateY(7px)" }, to: { opacity: "1", transform: "none" } },
        "fade-up": { from: { opacity: "0", transform: "translateY(10px)" }, to: { opacity: "1", transform: "none" } },
        "pop-in": { from: { opacity: "0", transform: "scale(0.96) translateY(6px)" }, to: { opacity: "1", transform: "none" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "toast-in": { from: { opacity: "0", transform: "translateY(12px) scale(0.97)" }, to: { opacity: "1", transform: "none" } },
      },
    },
  },
  plugins: [],
}