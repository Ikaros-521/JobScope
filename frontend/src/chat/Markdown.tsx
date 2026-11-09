import { useRef, useState } from 'react'
import type { ComponentProps } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { safeUrl } from '../api'

function CodeBlock({ children, ...props }: ComponentProps<'pre'>) {
  const ref = useRef<HTMLPreElement>(null)
  const [copied, setCopied] = useState(false)
  async function copy() {
    const text = ref.current?.textContent ?? ''
    try { await navigator.clipboard.writeText(text) } catch { return }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }
  return <div className="md-code">
    <div className="md-code-bar"><button type="button" onClick={() => void copy()}>{copied ? '已复制' : '复制代码'}</button></div>
    <pre ref={ref} {...props}>{children}</pre>
  </div>
}

function link({ href, children }: ComponentProps<'a'>) {
  const target = typeof href === 'string' ? safeUrl(href) : undefined
  return target ? <a href={target} target="_blank" rel="noreferrer">{children}</a> : <span className="md-dead-link">{children}</span>
}

export function Markdown({ content }: { content: string }) {
  return <div className="md-body">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{ a: link, pre: CodeBlock }}
    >{content}</ReactMarkdown>
  </div>
}
