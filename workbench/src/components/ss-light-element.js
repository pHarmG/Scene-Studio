/**
 * Lit base that renders into light DOM so Cursor's element picker (and
 * other design-tool overlays) can select inner nodes. A shadow root
 * otherwise collapses every click onto the host — on this page that is
 * always <ss-app>.
 *
 * Component `static styles` must be wrapped in `@scope (<tag>)` and use
 * `:scope` instead of `:host`. Sheets are adopted onto `document` once
 * per class: adopting onto the host itself is not supported (only
 * Document and ShadowRoot implement adoptedStyleSheets), and assigning
 * sheets onto the host would also throw.
 *
 * Slotted shells (drawer, panel) stay on LitElement.
 */
import { LitElement } from "lit";

const adoptedCtors = new WeakSet();

export class SsLightElement extends LitElement {
  createRenderRoot() {
    const ctor = this.constructor;
    if (!adoptedCtors.has(ctor) && Array.isArray(ctor.elementStyles)) {
      adoptedCtors.add(ctor);
      const sheets = ctor.elementStyles
        .map((style) => (style instanceof CSSStyleSheet ? style : style.styleSheet))
        .filter(Boolean);
      if (sheets.length) {
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, ...sheets];
      }
    }
    return this;
  }
}
