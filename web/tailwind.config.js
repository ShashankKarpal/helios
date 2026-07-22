/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0B0D0C",
        surface: "#151917",
        hairline: "#232826",
        text: "#E8ECE9",
        muted: "#9AA49E",
        mint: "#7EE0B1",
        caution: "#FBBF24",
        alert: "#F87171",
      },
      fontFamily: {
        serif: ["ui-serif", "New York", "Georgia", "serif"],
        sans: [
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
      },
      borderRadius: {
        "2xl": "1.25rem",
      },
      maxWidth: {
        app: "42rem",
      },
    },
  },
  plugins: [],
};
