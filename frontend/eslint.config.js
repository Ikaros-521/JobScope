import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

// 只守真实缺陷（hooks 规则 + 明显错误），不碰风格：仓库是刻意的单行密排风格，
// Prettier 全仓格式化与它正面冲突，未引入（2026-11-28，见 audit 批次 2 决策）。
export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  {
    // 既有代码里 10 处 eslint-disable 是「依赖故意收窄」的意图标记：react-hooks v7 的
    // 检测变聪明后它们不再触发，但保留注释比删掉更能说明窄依赖是刻意的。
    linterOptions: { reportUnusedDisableDirectives: 'off' },
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // 仓库通篇 catch {}（角标静默等刻意设计）与 as any 断言，按既有约定放行
      'no-empty': ['error', { allowEmptyCatch: true }],
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // 全角空格（U+3000）在用户可见文案里是有意的排版，不是手误
      'no-irregular-whitespace': ['error', { skipStrings: true, skipTemplates: true, skipComments: true, skipRegExps: true, skipJSXText: true }],
    },
  },
)
