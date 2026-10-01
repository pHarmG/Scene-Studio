const STYLE_ID = "ha-preview-shell-styles";

const darkTokens = {
  "--ha-preview-font-family": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  "--ha-preview-text": "rgba(234, 235, 238, 0.98)",
  "--ha-preview-muted": "rgba(234, 235, 238, 0.7)",
  "--ha-preview-subtle": "rgba(234, 235, 238, 0.56)",
  "--ha-preview-primary": "#6a74d3",
  "--ha-preview-bg":
    "linear-gradient(180deg, rgba(8, 10, 24, 0.36), rgba(8, 10, 24, 0.48)), center / cover no-repeat fixed url('./assets/luminous-wave-bg.svg')",
  "--ha-preview-board-bg":
    "linear-gradient(180deg, rgba(27, 29, 38, 0.1), rgba(18, 19, 24, 0.04))",
  "--ha-preview-toolbar-bg": "rgba(255, 255, 255, 0.05)",
  "--ha-preview-toolbar-border": "rgba(234, 235, 238, 0.09)",
  "--ha-preview-toolbar-active-bg": "rgba(76, 104, 180, 0.26)",
  "--ha-preview-toolbar-active-border": "rgba(120, 151, 235, 0.52)",
  "--ha-preview-sidebar-bg": "rgba(25, 28, 45, 0.8)",
  "--ha-preview-sidebar-border": "rgba(234, 235, 238, 0.08)",
  "--ha-preview-content-max-width": "1180px",
  "--ha-preview-app-gutter": "28px",
  "--ha-preview-sidebar-width": "86px",
  "--ha-preview-top-offset": "20px",
  "--ha-preview-grid-gap": "17px",
  "--ha-preview-card-gap": "16px",
  "--ha-preview-shell-radius": "18px",
  "--ha-preview-card-min-width": "320px",
  "--ha-preview-page-max-width": "min(100%, 1400px)",
  "--ha-preview-page-padding": "0px",
  "--ha-preview-section-side-padding": "0px",
};

const lightTokens = {
  "--ha-preview-font-family": darkTokens["--ha-preview-font-family"],
  "--ha-preview-text": "#132033",
  "--ha-preview-muted": "rgba(19, 32, 51, 0.72)",
  "--ha-preview-subtle": "rgba(19, 32, 51, 0.56)",
  "--ha-preview-primary": "#516bda",
  "--ha-preview-bg":
    "radial-gradient(circle at 84% 10%, rgba(255, 221, 180, 0.72), transparent 26%), radial-gradient(circle at 8% 12%, rgba(157, 188, 255, 0.45), transparent 28%), linear-gradient(180deg, #eef3fb 0%, #dde6f4 44%, #d4ddeb 100%)",
  "--ha-preview-board-bg":
    "linear-gradient(180deg, rgba(255, 255, 255, 0.18), rgba(236, 242, 250, 0.08))",
  "--ha-preview-toolbar-bg": "rgba(255, 255, 255, 0.4)",
  "--ha-preview-toolbar-border": "rgba(82, 101, 140, 0.14)",
  "--ha-preview-toolbar-active-bg": "rgba(81, 107, 218, 0.16)",
  "--ha-preview-toolbar-active-border": "rgba(81, 107, 218, 0.34)",
  "--ha-preview-sidebar-bg": "rgba(255, 255, 255, 0.5)",
  "--ha-preview-sidebar-border": "rgba(82, 101, 140, 0.14)",
  "--ha-preview-content-max-width": "1180px",
  "--ha-preview-app-gutter": "28px",
  "--ha-preview-sidebar-width": "86px",
  "--ha-preview-top-offset": "20px",
  "--ha-preview-grid-gap": "17px",
  "--ha-preview-card-gap": "16px",
  "--ha-preview-shell-radius": "18px",
  "--ha-preview-card-min-width": "320px",
  "--ha-preview-page-max-width": "min(100%, 1400px)",
  "--ha-preview-page-padding": "0px",
  "--ha-preview-section-side-padding": "0px",
};

function themeTokens(themeName) {
  return themeName === "light" ? lightTokens : darkTokens;
}

export function ensureHaPreviewStyles(document) {
  if (document.getElementById(STYLE_ID)) {
    return;
  }

  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    :root {
      color-scheme: dark;
      font-family: var(--ha-preview-font-family);
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      min-height: 100%;
    }

    body.ha-preview-body {
      margin: 0;
      padding: 0;
      color: var(--ha-preview-text);
      font-family: var(--ha-preview-font-family);
      background: var(--ha-preview-bg);
      background-attachment: fixed;
    }

    .ha-preview-app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: var(--ha-preview-sidebar-width) minmax(0, 1fr);
      align-items: start;
    }

    .ha-preview-sidebar {
      min-height: 100vh;
      position: sticky;
      top: 0;
      border-right: 1px solid var(--ha-preview-sidebar-border);
      background: var(--ha-preview-sidebar-bg);
      backdrop-filter: blur(10px) saturate(1.08);
      -webkit-backdrop-filter: blur(10px) saturate(1.08);
      display: grid;
      justify-items: center;
      align-content: start;
      gap: 18px;
      padding: calc(var(--ha-preview-top-offset) + 4px) 14px 18px;
    }

    .ha-preview-sidebarDot {
      width: 36px;
      height: 36px;
      border-radius: 14px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      background: rgba(255, 255, 255, 0.04);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
    }

    .ha-preview-main {
      min-width: 0;
      padding: var(--ha-preview-top-offset) var(--ha-preview-app-gutter) 40px;
    }

    .ha-preview-page {
      display: grid;
      gap: 22px;
      align-items: start;
      width: min(100%, var(--ha-preview-content-max-width));
    }

    .ha-preview-pageHeader {
      display: grid;
      gap: 8px;
      max-width: 980px;
    }

    .ha-preview-pageHeader h1 {
      margin: 0;
      font-size: 26px;
      line-height: 1.06;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--ha-preview-text);
    }

    .ha-preview-pageHeader p {
      margin: 0;
      font-size: 16px;
      line-height: 1.4;
      color: var(--ha-preview-muted);
    }

    .ha-preview-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .ha-preview-toolbar button {
      border: 1px solid var(--ha-preview-toolbar-border);
      border-radius: 999px;
      background: var(--ha-preview-toolbar-bg);
      color: inherit;
      padding: 12px 18px;
      cursor: pointer;
      font: inherit;
      line-height: 1;
      backdrop-filter: blur(10px) saturate(1.15);
      -webkit-backdrop-filter: blur(10px) saturate(1.15);
      transition: background 120ms ease, border-color 120ms ease;
    }

    .ha-preview-toolbar button.active {
      background: var(--ha-preview-toolbar-active-bg);
      border-color: var(--ha-preview-toolbar-active-border);
    }

    .ha-preview-stage {
      display: grid;
      gap: 14px;
      align-items: start;
    }

    .ha-preview-caption {
      font-size: 13px;
      line-height: 1.35;
      color: var(--ha-preview-subtle);
    }

    .ha-preview-device {
      width: min(100%, var(--ha-preview-shell-width, 462px));
      min-width: 0;
    }

    .ha-preview-reviewGrid {
      display: grid;
      gap: 22px;
      align-items: start;
      padding: 24px;
    }

    .ha-preview-reviewScenario {
      display: grid;
      gap: 10px;
      align-items: start;
    }

    .ha-preview-reviewScenario.is-framed {
      width: min(100%, calc(var(--ha-preview-shell-width, 1320px) + 40px));
      padding: 20px;
      border-radius: 26px;
      background: var(--ha-preview-bg);
      background-attachment: fixed;
      overflow: hidden;
    }

    .ha-preview-board {
      width: min(100%, var(--ha-preview-shell-width, 1320px));
      min-width: 0;
    }

    .ha-preview-huiView {
      width: 100%;
      min-width: 0;
    }

    .ha-preview-sectionsView {
      width: 100%;
      min-width: 0;
    }

    .ha-preview-sectionsWrapper {
      width: 100%;
      min-width: 0;
    }

    .ha-preview-sectionsContainer {
      width: 100%;
      min-width: 0;
      padding: var(--ha-preview-page-padding);
    }

    .ha-preview-sectionsContent {
      width: 100%;
      min-width: 0;
      display: grid;
      grid-template-columns: repeat(var(--ha-preview-board-columns, 2), minmax(var(--ha-preview-card-min-width), 1fr));
      gap: var(--ha-preview-grid-gap);
      align-items: start;
      background: var(--ha-preview-board-bg);
      border-radius: calc(var(--ha-preview-shell-radius) + 4px);
    }

    .ha-preview-boardSection {
      display: grid;
      gap: var(--ha-preview-card-gap);
      min-width: 0;
      align-content: start;
      padding-inline: var(--ha-preview-section-side-padding);
    }

    .ha-preview-boardSection > * {
      min-width: 0;
      width: 100%;
    }

    @media (max-width: 900px) {
      .ha-preview-app {
        grid-template-columns: 1fr;
      }

      .ha-preview-sidebar {
        display: none;
      }

      .ha-preview-main {
        padding: 20px 16px 32px;
      }

      .ha-preview-sectionsContent {
        grid-template-columns: minmax(0, 1fr);
      }
    }
  `;

  document.head.appendChild(style);
}

export function applyHaPreviewTheme(target, themeName = "dark") {
  const tokens = themeTokens(themeName);
  for (const [name, value] of Object.entries(tokens)) {
    target.style.setProperty(name, value);
  }
}

export function setupHaPreviewPage(document, { title, subtitle, theme = "dark" }) {
  ensureHaPreviewStyles(document);
  document.body.classList.add("ha-preview-body");
  applyHaPreviewTheme(document.body, theme);

  document.body.innerHTML = `
    <div class="ha-preview-app">
      <aside class="ha-preview-sidebar" aria-hidden="true">
        <div class="ha-preview-sidebarDot"></div>
        <div class="ha-preview-sidebarDot"></div>
        <div class="ha-preview-sidebarDot"></div>
        <div class="ha-preview-sidebarDot"></div>
      </aside>
      <main class="ha-preview-main">
        <div class="ha-preview-page">
          <header class="ha-preview-pageHeader">
            <h1>${title}</h1>
            <p>${subtitle}</p>
          </header>
          <div class="ha-preview-toolbar" id="toolbar"></div>
          <section class="ha-preview-stage">
            <div id="shell"></div>
            <div class="ha-preview-caption" id="caption"></div>
          </section>
        </div>
      </main>
    </div>
  `;

  return {
    toolbar: document.getElementById("toolbar"),
    shell: document.getElementById("shell"),
    caption: document.getElementById("caption"),
  };
}

export function setShellTheme(element, themeName = "dark") {
  applyHaPreviewTheme(element, themeName);
}

export function configureDeviceShell(element, { width, theme = "dark" }) {
  element.className = "ha-preview-device";
  element.style.setProperty("--ha-preview-shell-width", `${width}px`);
  setShellTheme(element, theme);
}

export function createBoardShell(document, { width, theme = "dark", columns = 2 }) {
  const board = document.createElement("div");
  board.className = "ha-preview-board";
  board.style.setProperty("--ha-preview-shell-width", `${width}px`);
  board.style.setProperty("--ha-preview-board-columns", String(columns));
  setShellTheme(board, theme);

  const huiView = document.createElement("div");
  huiView.className = "ha-preview-huiView";

  const sectionsView = document.createElement("div");
  sectionsView.className = "ha-preview-sectionsView";

  const wrapper = document.createElement("div");
  wrapper.className = "ha-preview-sectionsWrapper";

  const container = document.createElement("div");
  container.className = "ha-preview-sectionsContainer";

  const content = document.createElement("div");
  content.className = "ha-preview-sectionsContent";

  container.appendChild(content);
  wrapper.appendChild(container);
  sectionsView.appendChild(wrapper);
  huiView.appendChild(sectionsView);
  board.appendChild(huiView);

  return board;
}

export function createBoardSection(document) {
  const section = document.createElement("div");
  section.className = "ha-preview-boardSection";
  return section;
}

export function getBoardContentRoot(board) {
  return board.querySelector(".ha-preview-sectionsContent");
}

export function setupReviewGrid(document, { theme = "dark" } = {}) {
  ensureHaPreviewStyles(document);
  document.body.classList.add("ha-preview-body");
  applyHaPreviewTheme(document.body, theme);
  document.body.innerHTML = `<div class="ha-preview-reviewGrid" id="reviewGrid"></div>`;
  return document.getElementById("reviewGrid");
}

export function createReviewScenario(document, { id, width, theme = "dark", caption, wrapperId, framed = false } = {}) {
  const wrapper = document.createElement("section");
  wrapper.className = "ha-preview-reviewScenario";
  if (wrapperId) {
    wrapper.id = wrapperId;
  }
  if (framed) {
    wrapper.classList.add("is-framed");
    setShellTheme(wrapper, theme);
    wrapper.style.setProperty("--ha-preview-shell-width", `${width}px`);
  }

  if (caption) {
    const label = document.createElement("div");
    label.className = "ha-preview-caption";
    label.textContent = caption;
    wrapper.appendChild(label);
  }

  const shell = document.createElement("div");
  shell.id = id;
  configureDeviceShell(shell, { width, theme });
  wrapper.appendChild(shell);

  return { wrapper, shell };
}
