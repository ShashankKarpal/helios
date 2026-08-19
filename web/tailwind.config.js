/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0B0C0D",
        surface: "#171614",
        hairline: "#292826",
        text: "#F3F1EB",
        muted: "#8F8C85",
        mint: "#BFB287",
        caution: "#E0B93A",
        alert: "#CB5B45",
        mintPrint: "#4D4323",
        mutedPrint: "#5A5852",
      },
      fontFamily: {
        // Montserrat everywhere, per owner preference. Both the display face
        // (font-serif, used for greetings and verdicts) and body (font-sans)
        // resolve to Montserrat, with graceful fallbacks.
        serif: ["Montserrat", "ui-serif", "Georgia", "serif"],
        sans: ["Montserrat", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
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
