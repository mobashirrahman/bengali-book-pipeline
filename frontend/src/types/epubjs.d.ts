declare module 'epubjs' {
  interface EpubBook {
    ready: Promise<unknown>
    destroy?: () => void
  }

  interface EpubRendition {
    display: (target?: string) => Promise<unknown>
    destroy?: () => void
  }

  interface EpubOptions {
    width?: string | number
    height?: string | number
    flow?: 'paginated' | 'scrolled'
  }

  interface EpubBookFactory {
    (url: string): EpubBook & { renderTo: (element: HTMLElement, options: EpubOptions) => EpubRendition }
  }

  const ePub: EpubBookFactory
  export default ePub
}
