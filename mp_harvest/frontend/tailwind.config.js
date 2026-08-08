/** @type {import('tailwindcss').Config} */
// 设计 Token（设计稿 §5.2）映射：颜色全部走 CSS 变量，暗色由 prefers-color-scheme 切换（见 style.css）
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        app: 'var(--bg-app)',
        panel: 'var(--bg-panel)',
        sidebar: 'var(--bg-sidebar)',
        hover: 'var(--bg-hover)',
        selected: 'var(--bg-selected)',
        line: 'var(--border)',
        primary: 'var(--text-primary)',
        secondary: 'var(--text-secondary)',
        tertiary: 'var(--text-tertiary)',
        accent: 'var(--accent)',
        'accent-hover': 'var(--accent-hover)',
        success: 'var(--success)',
        warning: 'var(--warning)',
        danger: 'var(--danger)',
        info: 'var(--info)',
      },
      fontFamily: {
        ui: 'var(--font-ui)',
        mono: 'var(--font-mono)',
      },
      fontSize: {
        xs: 'var(--fs-xs)',
        sm: 'var(--fs-sm)',
        md: 'var(--fs-md)',
        lg: 'var(--fs-lg)',
        xl: 'var(--fs-xl)',
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
      },
      transitionTimingFunction: { ease: 'var(--ease)' },
      transitionDuration: { fast: 'var(--dur-fast)', med: 'var(--dur-med)' },
    },
  },
  plugins: [],
}
