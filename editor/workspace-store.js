import { createStore } from 'zustand/vanilla';

export const defaultLayout = { 'pane-library': 22, 'pane-preview': 49, 'pane-properties': 29 };
const layoutKey = 'xyj-editor-layout/v1';

function savedLayout() {
  try {
    const value = JSON.parse(localStorage.getItem(layoutKey));
    if (
      !value ||
      !Object.keys(defaultLayout).every((key) => Number.isFinite(value[key]) && value[key] > 0)
    )
      return defaultLayout;
    const entries = Object.keys(defaultLayout).map((key) => [key, value[key]]);
    return Math.abs(entries.reduce((sum, [, size]) => sum + size, 0) - 100) < 0.01
      ? Object.fromEntries(entries)
      : defaultLayout;
  } catch {
    return defaultLayout;
  }
}

export const workspaceStore = createStore(() => ({
  library: 'items',
  mobilePane: 'library',
  layout: savedLayout(),
  notice: { text: '正在读取数据…', error: false },
  itemFilters: { query: '', category: 'all', onlyItems: true },
  items: [],
  fields: [],
  edits: [],
  revision: 0,
  details: new Map(),
  selected: null,
  draftTarget: null,
  dirty: false,
  busy: false,
  loading: false,
  mode: 'view',
  formBaseline: null,
  choosingTemplate: false,
  baselineIds: new Set(),
  idsLoaded: false,
  ready: false,
  page: 0,
  ticket: 0,
  previewTicket: 0,
  previewMode: 'equipped',
}));

/** Give existing editor controllers a property-style view of the shared Zustand store. */
export const state = new Proxy(
  {},
  {
    get: (_target, key) => workspaceStore.getState()[key],
    set(_target, key, value) {
      if (!Object.is(workspaceStore.getState()[key], value))
        workspaceStore.setState({ [key]: value });
      return true;
    },
  }
);

/** Persist only layout preferences. */
export function saveLayout(layout) {
  state.layout = layout;
  try {
    localStorage.setItem(layoutKey, JSON.stringify(layout));
  } catch {
    /* Storage is optional. */
  }
}
