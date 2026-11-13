import { GripVertical } from 'lucide-react';
import { Group, Panel, Separator } from 'react-resizable-panels';

/** shadcn Resizable group using the current react-resizable-panels API. */
export function ResizableGroup({ className, ...props }) {
  return (
    <Group
      data-slot="resizable-panel-group"
      orientation="horizontal"
      className={className}
      {...props}
    />
  );
}

/** Expose native panel constraints to each workspace. */
export function ResizablePanel(props) {
  return <Panel data-slot="resizable-panel" {...props} />;
}

/** Provide a visible and keyboard-operable panel boundary. */
export function ResizableHandle({ label }) {
  return (
    <Separator data-slot="resizable-handle" aria-label={label} className="resize-handle">
      <GripVertical aria-hidden="true" />
    </Separator>
  );
}
