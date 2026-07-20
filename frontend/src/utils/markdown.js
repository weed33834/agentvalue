/**
 * Markdown 渲染工具
 *
 * 使用 marked 解析 + DOMPurify 消毒 + highlight.js 代码高亮。
 * 替换 AdminMultiAgent.vue 里的正则手写版（无库、无 XSS 防护）。
 *
 * 移植对应：opencode TUI 的 Markdown 渲染（虽然技术栈不同，但功能点一致）：
 * - 标题/列表/粗斜体/行内代码
 * - 代码块 + 语法高亮
 * - 表格/链接/引用块
 * - XSS 消毒
 *
 * 性能优化：只引入 highlight.js/lib/core + 常用语言注册，
 * 避免全量引入导致 chunk 膨胀（全量 ~1MB，按需 ~60KB）。
 *
 * 增强（P0-9）：
 * - KaTeX 数学公式渲染（$...$ 行内, $$...$$ 块级）
 * - Mermaid 图表渲染（```mermaid 代码块）
 */

import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import 'highlight.js/styles/github.css'
import katex from 'katex'
import 'katex/dist/katex.min.css'

// 按需注册常用语言（覆盖对话场景绝大多数代码片段）
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import shell from 'highlight.js/lib/languages/shell'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import sql from 'highlight.js/lib/languages/sql'
import markdown from 'highlight.js/lib/languages/markdown'
import yaml from 'highlight.js/lib/languages/yaml'
import go from 'highlight.js/lib/languages/go'
import rust from 'highlight.js/lib/languages/rust'
import java from 'highlight.js/lib/languages/java'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('shell', shell)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('go', go)
hljs.registerLanguage('rust', rust)
hljs.registerLanguage('java', java)
hljs.registerLanguage('c', c)
hljs.registerLanguage('cpp', cpp)
hljs.registerLanguage('ts', typescript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('py', python)
hljs.registerLanguage('sh', shell)
hljs.registerLanguage('yml', yaml)

// 配置 marked
marked.setOptions({
  breaks: true,
  gfm: true,
})

// ============================================================
// 数学公式预处理（KaTeX）
// ============================================================

const MATH_PLACEHOLDER_PREFIX = '\u0000MATH'
const mathCache = []

/**
 * 预处理：提取 $$...$$ 和 $...$ 数学表达式，替换为占位符。
 * 避免被 marked 解析破坏。
 */
function preprocessMath(text) {
  mathCache.length = 0
  if (!text) return text

  // 1. 先处理块级公式 $$...$$
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, formula) => {
    const idx = mathCache.length
    try {
      const rendered = katex.renderToString(formula.trim(), {
        displayMode: true,
        throwOnError: false,
        output: 'html',
      })
      mathCache.push(rendered)
    } catch (e) {
      mathCache.push(`<span class="math-error">${escapeHtml(match)}</span>`)
    }
    return `${MATH_PLACEHOLDER_PREFIX}${idx}\u0000`
  })

  // 2. 再处理行内公式 $...$（排除 \$ 转义）
  text = text.replace(/(?<!\\)\$([^\n$]+?)\$/g, (match, formula) => {
    // 跳过明显不是公式的（如价格 $5）
    if (/^\d/.test(formula.trim())) return match
    const idx = mathCache.length
    try {
      const rendered = katex.renderToString(formula.trim(), {
        displayMode: false,
        throwOnError: false,
        output: 'html',
      })
      mathCache.push(rendered)
    } catch (e) {
      mathCache.push(`<span class="math-error">${escapeHtml(match)}</span>`)
    }
    return `${MATH_PLACEHOLDER_PREFIX}${idx}\u0000`
  })

  return text
}

/**
 * 后处理：将占位符替换回 katex 渲染后的 HTML。
 */
function postprocessMath(html) {
  return html.replace(
    new RegExp(`${MATH_PLACEHOLDER_PREFIX}(\\d+)\u0000`, 'g'),
    (match, idx) => mathCache[parseInt(idx)] || match,
  )
}

// 自定义 renderer：代码块加 highlight.js 高亮 + Mermaid 检测
const renderer = new marked.Renderer()
renderer.code = function (code, lang) {
  // marked v14 传 (code, lang)，旧版传 ({text, lang})
  if (typeof code === 'object') {
    lang = code.lang
    code = code.text
  }

  // Mermaid 图表：输出 div.mermaid，由后续 mermaid.run() 渲染
  if (lang === 'mermaid') {
    return `<div class="mermaid">${escapeHtml(code)}</div>`
  }

  const language = lang && hljs.getLanguage(lang) ? lang : null
  try {
    if (language) {
      const highlighted = hljs.highlight(code, { language }).value
      return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`
    }
    // 未注册语言：不自动识别（highlightAuto 会尝试所有已注册语言，性能尚可）
    const highlighted = hljs.highlightAuto(code).value
    return `<pre><code class="hljs">${highlighted}</code></pre>`
  } catch {
    return `<pre><code class="hljs">${escapeHtml(code)}</code></pre>`
  }
}
marked.use({ renderer })

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

// ============================================================
// Mermaid 懒加载
// ============================================================

let _mermaidLoaded = false
let _mermaidPromise = null

/**
 * 懒加载 mermaid 并渲染页面中所有 .mermaid 元素。
 * 在 MessageBubble 的 onMounted / watch 中调用。
 */
export async function renderMermaid(container) {
  if (!container) return
  const mermaidEls = container.querySelectorAll('.mermaid:not([data-processed])')
  if (mermaidEls.length === 0) return

  if (!_mermaidLoaded) {
    if (!_mermaidPromise) {
      _mermaidPromise = import('mermaid').then((mod) => {
        const mermaid = mod.default
        mermaid.initialize({
          startOnLoad: false,
          theme: 'neutral',
          securityLevel: 'loose',
        })
        _mermaidLoaded = true
        return mermaid
      })
    }
  }

  try {
    const mermaid = await _mermaidPromise
    // mermaid v10+ 使用 run()，v9 使用 mermaid.init()
    if (mermaid.run) {
      await mermaid.run({
        nodes: mermaidEls,
      })
    } else {
      mermaid.init(undefined, mermaidEls)
    }
    // 标记已处理
    mermaidEls.forEach((el) => el.setAttribute('data-processed', 'true'))
  } catch (e) {
    console.warn('Mermaid 渲染失败:', e)
    mermaidEls.forEach((el) => {
      el.classList.add('mermaid-error')
      el.setAttribute('data-processed', 'true')
    })
  }
}

/**
 * 渲染 Markdown 为安全的 HTML
 * @param {string} text - Markdown 文本
 * @returns {string} 消毒后的 HTML
 */
export function renderMarkdown(text) {
  if (!text) return ''
  // 1. 预处理数学公式
  const preprocessed = preprocessMath(text)
  // 2. marked 解析
  const html = marked.parse(preprocessed)
  // 3. 后处理：替换数学占位符
  const withMath = postprocessMath(html)
  // 4. DOMPurify 消毒
  return DOMPurify.sanitize(withMath, {
    ALLOWED_TAGS: [
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'p', 'br', 'hr',
      'strong', 'em', 'del', 's', 'u',
      'ul', 'ol', 'li',
      'blockquote', 'code', 'pre',
      'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'span', 'div', 'sup', 'sub',
      // KaTeX 输出需要的标签
      'math', 'semantics', 'annotation', 'mrow', 'mi', 'mo', 'mn', 'msup', 'msub', 'mfrac', 'msqrt', 'mroot', 'mtext', 'mspace', 'mtable', 'mtr', 'mtd', 'mover', 'munder', 'munderover',
    ],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'style', 'encoding', 'xmlns', 'mathvariant', 'fontstyle', 'fontweight', 'lspace', 'rspace', 'stretchy', 'symmetric', 'maxsize', 'minsize', 'fence', 'separator', 'accent', 'accentunder', 'columnalign', 'rowalign', 'columnspacing', 'rowspacing', 'columnlines', 'rowlines', 'frame', 'framespacing', 'equalrows', 'side', 'stackalign', 'denomalign', 'numalign', 'bevelled', 'linethickness', 'notation', 'subscriptshift', 'supershift'],
  })
}

export { marked, DOMPurify, hljs, katex }
