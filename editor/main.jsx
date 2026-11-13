import { memo, useRef } from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import {
  Box,
  ClipboardList,
  List,
  PanelRight,
  RotateCcw,
  Settings,
  Sword,
  Swords,
  X,
} from 'lucide-react';
import { animate } from 'motion/mini';
import { useStore } from 'zustand';
import { Button } from './ui/controls.jsx';
import { ResizableGroup, ResizableHandle, ResizablePanel } from './ui/resizable.jsx';
import { defaultLayout, saveLayout, state, workspaceStore } from './workspace-store.js';
import {
  CreatureDraftContent,
  CreatureLibrary,
  CreatureProperties,
  DiscardDialog,
  ItemDraftContent,
  ItemLibrary,
  ItemProperties,
  PreviewContent,
  PublicationDialog,
  SettingsContent,
} from './workspace-content.jsx';
import './style.css';

const Libraries = memo(function Libraries() {
  return (
    <>
      <ItemLibrary />
      <CreatureLibrary />
    </>
  );
});
const Properties = memo(function Properties() {
  return (
    <>
      <ItemProperties />
      <CreatureProperties />
    </>
  );
});
const Preview = memo(PreviewContent);
const SettingsPage = memo(SettingsContent);

const tabs = [
  { key: 'library', label: '列表', Icon: List },
  { key: 'preview', label: '预览', Icon: Box },
  { key: 'properties', label: '属性', Icon: PanelRight },
];
const desktopLayout = () => matchMedia('(min-width: 1000px)').matches;

function Notice() {
  const notice = useStore(workspaceStore, (value) => value.notice);
  return (
    <div
      id="status"
      className={notice.error ? 'error' : undefined}
      role="status"
      hidden={!notice.text}
    >
      {notice.text}
    </div>
  );
}

function Toolbar({ group, openDrafts }) {
  const library = useStore(workspaceStore, (value) => value.library);
  return (
    <header className="workshop-header">
      <div className="workshop-title">
        <Sword aria-hidden="true" />
        <h1>道具工坊</h1>
      </div>
      <nav className="library-switch" aria-label="资源类型">
        <Button id="library-items" variant="ghost" aria-pressed={library === 'items'}>
          <Sword aria-hidden="true" />
          道具
        </Button>
        <Button id="library-creatures" variant="ghost" aria-pressed={library === 'creatures'}>
          <Swords aria-hidden="true" />
          NPC 与怪物
        </Button>
        <span id="creature-availability-note" role="status" />
      </nav>
      <div className="header-actions">
        <Button
          variant="ghost"
          size="icon"
          className="layout-reset"
          title="复位面板"
          aria-label="复位面板"
          onClick={() => {
            if (group.current) saveLayout(group.current.setLayout(defaultLayout));
          }}
        >
          <RotateCcw aria-hidden="true" />
        </Button>
        <Button variant="ghost" onClick={openDrafts}>
          <ClipboardList aria-hidden="true" />
          草稿
        </Button>
        <Button
          id="open-settings"
          variant="ghost"
          size="icon"
          title="设置"
          aria-label="设置"
          aria-expanded="false"
          aria-controls="settings-panel"
        >
          <Settings aria-hidden="true" />
        </Button>
        <Button id="recover-publication" hidden>
          恢复发布
        </Button>
        <Button id="export" variant="default" disabled hidden={library === 'creatures'}>
          发布道具
        </Button>
        <Button id="npc-export" variant="default" disabled hidden={library !== 'creatures'}>
          发布怪物
        </Button>
      </div>
    </header>
  );
}

const DraftDialog = memo(function DraftDialog({ dialog, sheet }) {
  const library = useStore(workspaceStore, (value) => value.library);
  const error = useStore(workspaceStore, (value) => (value.notice.error ? value.notice.text : ''));
  return (
    <dialog
      className="draft-dialog"
      id="draft-window"
      ref={dialog}
      aria-labelledby="draft-window-title"
      data-library={library}
    >
      <div ref={sheet} className="draft-sheet">
        <div className="section-title">
          <h2 id="draft-window-title">{library === 'items' ? '道具草稿' : '怪物草稿'}</h2>
          <Button
            variant="ghost"
            size="icon"
            aria-label="关闭草稿"
            title="关闭草稿"
            onClick={() => dialog.current.close()}
          >
            <X aria-hidden="true" />
          </Button>
        </div>
        <DraftPanes />
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </div>
    </dialog>
  );
});

const DraftPanes = memo(function DraftPanes() {
  return (
    <>
      <ItemDraftContent />
      <CreatureDraftContent />
    </>
  );
});

function MobileTabs() {
  const active = useStore(workspaceStore, (value) => value.mobilePane);
  return (
    <nav className="mobile-tabs" aria-label="工作区面板">
      {tabs.map(({ key, label, Icon }) => (
        <Button
          key={key}
          variant="ghost"
          aria-controls={`workspace-${key}`}
          aria-pressed={active === key}
          onClick={() => {
            state.mobilePane = key;
          }}
        >
          <Icon aria-hidden="true" />
          {label}
        </Button>
      ))}
    </nav>
  );
}

function Workspace({ group }) {
  const pane = useStore(workspaceStore, (value) => value.mobilePane);
  const layout = useRef(state.layout);
  return (
    <main id="workspace-main" tabIndex={-1} data-mobile-pane={pane}>
      <MobileTabs />
      <ResizableGroup
        id="editor-layout"
        className="workshop-panels"
        groupRef={group}
        defaultLayout={layout.current}
        onLayoutChanged={(sizes, event) => {
          if (desktopLayout() && event?.isUserInteraction)
            saveLayout(event.requestedLayout || sizes);
        }}
      >
        <ResizablePanel
          id="pane-library"
          className="workbench-panel pane-library"
          minSize="205px"
          defaultSize="22%"
        >
          <div id="workspace-library" className="panel-scroll">
            <Libraries />
          </div>
        </ResizablePanel>
        <ResizableHandle label="调整列表与预览宽度" />
        <ResizablePanel
          id="pane-preview"
          className="workbench-panel pane-preview"
          minSize="280px"
          defaultSize="49%"
        >
          <div id="workspace-preview" className="panel-scroll">
            <Preview />
          </div>
        </ResizablePanel>
        <ResizableHandle label="调整预览与属性宽度" />
        <ResizablePanel
          id="pane-properties"
          className="workbench-panel pane-properties"
          minSize="285px"
          defaultSize="29%"
        >
          <div id="workspace-properties" className="panel-scroll">
            <Properties />
          </div>
        </ResizablePanel>
      </ResizableGroup>
    </main>
  );
}

function Workshop() {
  const group = useRef(null);
  const draftDialog = useRef(null);
  const sheet = useRef(null);
  function openDrafts() {
    draftDialog.current.showModal();
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
      animate(sheet.current, { opacity: [0.45, 1] }, { duration: 0.14, ease: 'easeOut' });
    }
  }
  return (
    <>
      <a
        href="#workspace-main"
        className="skip"
        onClick={() => {
          state.mobilePane = 'properties';
        }}
      >
        跳到工作区
      </a>
      <Toolbar group={group} openDrafts={openDrafts} />
      <Notice />
      <SettingsPage />
      <Workspace group={group} />
      <DraftDialog dialog={draftDialog} sheet={sheet} />
      <DiscardDialog />
      <PublicationDialog />
    </>
  );
}

const root = createRoot(document.getElementById('root'));
flushSync(() => root.render(<Workshop />));
import('./app.js').catch((error) => {
  state.notice = { text: `工坊启动失败：${error.message}`, error: true };
});
