// 桌面外壳（pywebview）桥接：打开外部链接、复制到剪贴板。
//
// 为什么需要这一层（2026-09 实测）：
// 1) 「打开」原先用 window.open(url, '_blank')。pywebview 的 Cocoa 实现只在
//    WKNavigationTypeLinkActivated（真人点了 <a>）时才把链接交给系统浏览器
//    —— 见 webview/platforms/cocoa.py 的
//    webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_；
//    而 JS 的 window.open 产生的 navigationType 是 Other，既不开窗也不报错，
//    界面上就是「点了没反应」。必须走 shell 侧的 js_api（webbrowser.open）。
// 2) 「复制」原先直接 await navigator.clipboard.writeText，没有 try/catch。
//    WKWebView 里该 API 可能因权限被 rejected，于是要么静默失败、要么更糟：
//    没 await 的调用点会照样弹「已复制」。这里统一兜底并返回真实结果。

interface PywebviewApi {
  open_external?: (url: string) => unknown
  choose_directory?: () => Promise<string>
  choose_file?: (kind?: string) => Promise<string>
}

function shellApi(): PywebviewApi | undefined {
  const w = window as unknown as { pywebview?: { api?: PywebviewApi } }
  return w.pywebview?.api
}

/** 在系统浏览器中打开链接；返回是否已发起（调用方据此提示失败）。 */
export function openExternal(url: string): boolean {
  const target = String(url || '').trim()
  if (!target) return false

  const bridge = shellApi()
  if (bridge?.open_external) {
    try {
      bridge.open_external(target)
      return true
    } catch {
      /* 落到下面的兜底 */
    }
  }

  // 浏览器 / 开发模式兜底：真实 <a target="_blank">。
  // 即便在 pywebview 下，anchor 点击也会被判定为 LinkActivated 而被接住，
  // 比 window.open 可靠。
  try {
    const a = document.createElement('a')
    a.href = target
    a.target = '_blank'
    a.rel = 'noopener noreferrer'
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    a.remove()
    return true
  } catch {
    return false
  }
}

/**
 * 弹出系统目录选择器；返回绝对路径。
 *
 * 返回 ``null`` 区分三种情况，调用方据此决定是否提示：
 * - 用户取消（bridge 返回空串）→ ``null``，不是错误，静默即可
 * - 当前环境没有 pywebview 桥（浏览器 / 开发模式）→ ``null`` + ``reason``
 * - 选择过程抛异常 → ``null`` + ``reason``（把原因交给调用方展示）
 *
 * 抽到这里是因为原先只有设置页内联了这一套（2026-09），「其他来源」也要用，
 * 再抄一份就会出现两处各自演化。
 */
export async function chooseDirectory(): Promise<{ path: string | null; reason?: string }> {
  const bridge = shellApi()
  if (!bridge?.choose_directory) {
    return { path: null, reason: '当前环境不支持原生目录选择，请手动输入路径' }
  }
  try {
    const dir = await bridge.choose_directory()
    return { path: dir ? String(dir) : null }
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e)
    return { path: null, reason: `目录选择失败：${why}（可手动输入路径）` }
  }
}

/**
 * 选单个文件（周报模板用）；返回绝对路径。
 *
 * 与 ``chooseDirectory`` 同形：``null`` + ``reason`` 表示要提示，
 * ``null`` 无 reason 表示用户取消。
 */
export async function chooseFile(
  kind: 'html' | 'any' = 'html',
): Promise<{ path: string | null; reason?: string }> {
  const bridge = shellApi()
  if (!bridge?.choose_file) {
    return { path: null, reason: '当前环境不支持原生文件选择，请手动输入路径' }
  }
  try {
    const picked = await bridge.choose_file(kind)
    return { path: picked ? String(picked) : null }
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e)
    return { path: null, reason: `文件选择失败：${why}（可手动输入路径）` }
  }
}

/** 复制文本；返回是否真的复制成功（而非「已发起」）。 */
export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? '')

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    /* 继续走兜底 */
  }

  // 兜底：隐藏 textarea + execCommand（WKWebView 下通常可用）
  try {
    const ta = document.createElement('textarea')
    ta.value = value
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, value.length)
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}
