import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/** On-demand Three.js viewport; replacing models disposes every GPU resource. */
export class Viewer {
  constructor(host) {
    this.host = host;
    this.renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    host.append(this.renderer.domElement);
    this.renderer.domElement.setAttribute('aria-label', '三维模型，可使用下方按钮旋转和缩放');
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, .01, 10000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x778888, 2.3));
    const light = new THREE.DirectionalLight(0xffffff, 2);
    light.position.set(3, 5, 7);
    this.scene.add(light);
    this.group = new THREE.Group();
    this.scene.add(this.group);
    this.ticket = 0;
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
    this.reset();
  }
  /** Fit the canvas to its CSS viewport without changing layout. */
  resize() {
    const {width, height} = this.host.getBoundingClientRect();
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / Math.max(1, height);
    this.camera.updateProjectionMatrix();
    this.render();
  }
  /** Release the old mesh, materials and textures before showing another item. */
  clear() {
    this.ticket++;
    for (const mesh of [...this.group.children]) {
      mesh.geometry.dispose();
      mesh.material.map?.dispose();
      mesh.material.dispose();
      this.group.remove(mesh);
    }
    this.render();
  }
  /** Load original indexed meshes with their original TEX textures; report failures visibly. */
  async show(model, warning) {
    this.clear();
    const ticket = this.ticket;
    for (const data of model.meshes) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(data.positions, 3));
      geometry.setAttribute('uv', new THREE.Float32BufferAttribute(data.uvs, 2));
      geometry.setIndex(data.indices);
      geometry.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({color: 0xffffff, roughness: .9, metalness: 0, side: THREE.DoubleSide});
      const mesh = new THREE.Mesh(geometry, material);
      this.group.add(mesh);
      if (data.texture_url || data.texture !== null) {
        new THREE.TextureLoader().load(data.texture_url || `/api/texture/${data.texture}.png`, texture => {
          if (this.ticket !== ticket) { texture.dispose(); return; }
          texture.colorSpace = THREE.SRGBColorSpace;
          // The original Direct3D assets address image rows from the top.
          texture.flipY = false;
          texture.magFilter = THREE.NearestFilter;
          texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
          material.map = texture;
          material.alphaTest = .5;
          material.needsUpdate = true;
          this.render();
        }, undefined, () => {
          if (this.ticket === ticket) warning(`人物或装备贴图读取失败，当前显示未贴图网格。`);
        });
      }
    }
    // Convert native Direct3D's left-handed coordinates into Three.js coordinates.
    // Reflect Z and view from +Z so native left/right hands stay on the correct side.
    this.group.scale.set(model.action.scale[0], model.action.scale[1], -model.action.scale[2]);
    this.group.position.set(0, 0, 0);
    this.group.rotation.set(0, 0, 0);
    const box = new THREE.Box3().setFromObject(this.group);
    const center = box.getCenter(new THREE.Vector3());
    this.group.position.sub(center);
    const size = box.getSize(new THREE.Vector3());
    this.distance = Math.max(size.x, size.y, size.z, 1) * 2.05;
    this.reset();
  }
  /** Restore a reproducible front view and the full model framing. */
  reset() {
    const d = this.distance || 100;
    this.camera.position.set(d * .28, d * .10, d);
    this.camera.near = Math.max(.01, d / 1000);
    this.camera.far = d * 100;
    this.camera.updateProjectionMatrix();
    this.controls.target.set(0, 0, 0);
    this.controls.minDistance = d * .06;
    this.controls.maxDistance = d * 15;
    this.controls.update();
    this.render();
  }
  /** Keyboard-accessible alternative to dragging the model. */
  rotate(delta) { this.group.rotation.y += delta; this.render(); }
  /** Keyboard-accessible alternative to wheel zoom. */
  zoom(factor) {
    const offset = this.camera.position.clone().sub(this.controls.target).multiplyScalar(factor);
    offset.clampLength(this.controls.minDistance, this.controls.maxDistance);
    this.camera.position.copy(this.controls.target).add(offset);
    this.controls.update();
    this.render();
  }
  /** Toggle polygon inspection without changing game assets. */
  wireframe(value) { for (const mesh of this.group.children) mesh.material.wireframe = value; this.render(); }
  /** Render only on view changes, resize and resource completion. */
  render() { this.renderer.render(this.scene, this.camera); }
  /** Release observers, controls and GPU state when the page is closed. */
  dispose() { this.clear(); this.observer.disconnect(); this.controls.dispose(); this.renderer.dispose(); }
}
