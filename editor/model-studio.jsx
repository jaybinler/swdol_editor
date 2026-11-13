import { Button, Input, NativeSelect } from './ui/controls.jsx';
import { ResizableGroup, ResizablePanel, ResizableHandle } from './ui/resizable.jsx';

function VectorFields({ label, prefix, initial }) {
  return (
    <fieldset className="model-vector">
      <legend>{label}</legend>
      {['X', 'Y', 'Z'].map((axis, index) => (
        <label key={axis}>
          {axis}
          <Input id={prefix + '-' + index} type="number" defaultValue={initial} step="any" />
        </label>
      ))}
    </fieldset>
  );
}

function TintFields() {
  return ['红', '绿', '蓝'].map((name, index) => (
    <label key={name}>
      {name}
      <Input
        id={'model-tint-' + index}
        type="number"
        min="0"
        max="4"
        step="0.05"
        defaultValue="1"
      />
    </label>
  ));
}

/** Render the model editor around the existing Three.js authoring viewport. */
export function ModelStudio() {
  return (
    <>
      <header className="model-studio-header">
        <div>
          <h2 id="model-studio-title">怪物模型编辑</h2>
        </div>
        <div className="model-actions">
          <Button id="model-save" variant="default">
            保存模型草稿
          </Button>
          <Button id="model-close">返回怪物</Button>
        </div>
      </header>
      <div className="model-toolbar" aria-label="模型编辑工具">
        <div className="model-actions" id="model-modes">
          <Button data-mode="vertex" aria-pressed="true">
            顶点
          </Button>
          <Button data-mode="edge" aria-pressed="false">
            边
          </Button>
          <Button data-mode="face" aria-pressed="false">
            面
          </Button>
          <Button data-mode="bone" aria-pressed="false">
            骨骼
          </Button>
        </div>
        <div className="model-actions" id="model-transforms">
          <Button data-transform="translate" aria-pressed="true">
            移动
          </Button>
          <Button data-transform="rotate" aria-pressed="false">
            旋转
          </Button>
          <Button data-transform="scale" aria-pressed="false">
            缩放
          </Button>
        </div>
        <div className="model-actions">
          <Button id="model-undo">撤销</Button>
          <Button id="model-redo">重做</Button>
          <Button id="model-fit">适应视口</Button>
          <Button id="model-all">全选</Button>
          <Button id="model-none">取消选择</Button>
        </div>
      </div>
      <p id="model-studio-message" className="model-message" role="status" aria-live="polite"></p>
      <ResizableGroup id="model-layout" className="model-studio-body">
        <ResizablePanel
          id="model-tree-pane"
          className="model-editor-pane"
          defaultSize="20%"
          minSize="150px"
        >
          <aside className="model-tree">
            <label>
              动作模型<NativeSelect id="model-asset"></NativeSelect>
            </label>
            <p id="model-counts" className="property-note"></p>
            <h3>编辑骨骼</h3>
            <div
              id="model-bone-list"
              className="model-bone-list"
              role="list"
              aria-label="编辑骨骼"
            ></div>
            <div className="model-actions">
              <Button id="model-add-bone">新增骨骼</Button>
              <Button id="model-remove-bone">删除骨骼</Button>
            </div>
            <Button id="model-source">重新读取游戏模型</Button>
          </aside>
        </ResizablePanel>
        <ResizableHandle label="调整视口宽度" />
        <ResizablePanel
          id="model-canvas-pane"
          className="model-editor-pane model-canvas-pane"
          defaultSize="51%"
          minSize="240px"
        >
          <section className="model-canvas-column" aria-label="编辑和动作预览">
            <div id="model-canvas" className="model-canvas"></div>
            <div className="model-timeline">
              <label>
                动作<NativeSelect id="model-action"></NativeSelect>
              </label>
              <Button id="model-play" aria-pressed="false">
                播放
              </Button>
              <label>
                预览帧率
                <Input id="model-fps" type="number" min="1" max="60" defaultValue="10" />
              </label>
              <label className="model-frame-range">
                姿态
                <input id="model-frame" type="range" min="0" defaultValue="0" />
              </label>
              <output id="model-frame-number">0</output>
            </div>
            <div className="model-actions">
              <label className="model-check">
                <input id="model-wire" type="checkbox" defaultChecked />
                线框
              </label>
              <label className="model-check">
                <input id="model-show-bones" type="checkbox" />
                显示骨骼
              </label>
              <p className="property-note">
                点击选择 · Ctrl多选 · Shift拖动框选 · 右键平移 · 滚轮缩放
              </p>
            </div>
          </section>
        </ResizablePanel>
        <ResizableHandle label="调整模型属性宽度" />
        <ResizablePanel
          id="model-inspector-pane"
          className="model-editor-pane"
          defaultSize="29%"
          minSize="210px"
        >
          <aside className="model-inspector">
            <p id="model-selection" className="property-note"></p>
            <details open>
              <summary>顶点与拓扑</summary>
              <VectorFields label="绑定姿态坐标" prefix="model-position" initial="0" />
              <Button id="model-set-position">移动选中顶点中心到此坐标</Button>
              <VectorFields label="增量移动" prefix="model-move" initial="0" />
              <VectorFields label="旋转（度）" prefix="model-rotate" initial="0" />
              <VectorFields label="比例" prefix="model-scale" initial="1" />
              <Button id="model-apply-transform">应用选区变换</Button>
              <div className="model-actions model-topology">
                <Button data-operation="vertex">新增顶点</Button>
                <Button data-operation="face">新增三角面</Button>
                <Button data-operation="split">分割</Button>
                <Button data-operation="merge">合并顶点</Button>
                <Button data-operation="delete">删除选区</Button>
              </div>
              <label>
                挤出距离
                <Input id="model-extrude-distance" type="number" step="any" defaultValue="1" />
              </label>
              <Button id="model-extrude">挤出选中面</Button>
              <div className="model-two">
                <label>
                  U<Input id="model-u" type="number" step="any" defaultValue="0" />
                </label>
                <label>
                  V<Input id="model-v" type="number" step="any" defaultValue="0" />
                </label>
              </div>
              <Button id="model-set-uv">设置选中顶点UV</Button>
            </details>
            <details id="model-bone-panel">
              <summary>骨骼与绑定</summary>
              <label>
                当前骨骼<NativeSelect id="model-bone"></NativeSelect>
              </label>
              <label>
                名称
                <Input id="model-bone-name" maxLength="100" />
              </label>
              <label>
                父级<NativeSelect id="model-parent"></NativeSelect>
              </label>
              <label>
                动作来源<NativeSelect id="model-source-group"></NativeSelect>
              </label>
              <VectorFields label="绑定关节位置" prefix="model-pivot" initial="0" />
              <VectorFields label="骨骼移动" prefix="model-bone-position" initial="0" />
              <VectorFields label="骨骼旋转（度）" prefix="model-bone-rotation" initial="0" />
              <VectorFields label="骨骼比例" prefix="model-bone-scale" initial="1" />
              <label className="model-check">
                <input id="model-pose-key" type="checkbox" />
                只修改当前姿态
              </label>
              <Button id="model-set-bone">应用骨骼修改</Button>
              <Button id="model-delete-key">移除当前姿态修改</Button>
              <h4>选中顶点的骨骼权重</h4>
              <div id="model-weights"></div>
              <div className="model-actions">
                <Button id="model-add-weight">添加绑定</Button>
                <Button id="model-set-weights">应用权重</Button>
              </div>
              <p className="property-note">最多4根骨骼，权重合计为1。先选顶点，再设置绑定。</p>
            </details>
            <details open>
              <summary>材质与贴图</summary>
              <label>
                材质<NativeSelect id="model-material"></NativeSelect>
              </label>
              <Button id="model-assign-material">应用到选中面</Button>
              <img id="model-texture-preview" alt="游戏编码后的材质贴图" hidden />
              <div className="model-rgb">
                <TintFields />
              </div>
              <Button id="model-apply-tint">应用调色</Button>
              <label>
                导入PNG
                <input id="model-png" type="file" accept="image/png" />
              </label>
              <div className="model-actions">
                <Button id="model-download-texture">导出PNG</Button>
                <Button id="model-restore-texture">恢复来源贴图</Button>
              </div>
              <p className="property-note">
                预览为最终RGB555颜色；透明度按128分界转换为透明或不透明。PNG最大2048×2048。
              </p>
            </details>
          </aside>
        </ResizablePanel>
      </ResizableGroup>
    </>
  );
}
