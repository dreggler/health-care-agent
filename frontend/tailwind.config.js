const v = (name) => `rgb(var(--${name}) / <alpha-value>)`

export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: ['variant', [
    '@media (prefers-color-scheme: dark) { &:where(:not([data-theme="light"], [data-theme="light"] *)) }',
    '&:where([data-theme="dark"], [data-theme="dark"] *)',
  ]],
  theme: {
    extend: {
      colors: {
        bg: v('bg'),
        panel: v('panel'),
        sunken: v('sunken'),
        line: { DEFAULT: v('line'), 2: v('line-2') },
        ink: { DEFAULT: v('ink'), 2: v('ink-2'), 3: v('ink-3') },
        accent: { DEFAULT: v('accent'), strong: v('accent-strong'), ink: v('accent-ink') },
        ok: { DEFAULT: v('ok'), ink: v('ok-ink') },
        warn: { DEFAULT: v('warn'), ink: v('warn-ink') },
        bad: { DEFAULT: v('bad'), ink: v('bad-ink') },
        info: { DEFAULT: v('info'), ink: v('info-ink') },
        primary: {
          50: v('primary-50'),
          100: v('primary-100'),
          200: v('primary-200'),
          300: v('primary-300'),
          400: v('primary-400'),
          500: v('primary-500'),
          600: v('primary-600'),
          700: v('primary-700'),
          800: v('primary-800'),
          900: v('primary-900'),
        },
        chart: {
          heart: v('c-heart'),
          sleep: v('c-sleep'),
          activity: v('c-activity'),
          body: v('c-body'),
          vitals: v('c-vitals'),
          mind: v('c-mind'),
          env: v('c-env'),
          other: v('c-other'),
        },
        hime: {
          cream: '#FFF2DB',
          rose: '#E0808A',
          'rose-light': '#FFC7D4',
          warm: '#F9F5F0',
          brown: '#241F1A',
          leaf: '#5AAD50',
          sky: '#7AC4E8',
        },
      },
      borderRadius: {
        chip: 'var(--r-chip)',
        control: 'var(--r-control)',
        card: 'var(--r-card)',
      },
      boxShadow: {
        sm: 'var(--shadow-1)',
        DEFAULT: 'var(--shadow-1)',
        md: 'var(--shadow-1)',
        lg: 'var(--shadow-2)',
        xl: 'var(--shadow-2)',
        '2xl': 'var(--shadow-2)',
        inner: 'inset 0 1px 2px rgb(0 0 0 / 0.06)',
      },
      ringColor: { DEFAULT: v('accent') },
      borderColor: { DEFAULT: v('line') },
      divideColor: { DEFAULT: v('line') },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}
