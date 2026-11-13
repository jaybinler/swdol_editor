import * as THREE from 'three';

/** Preview native particle meshes at the weapon's two authored anchors. */
export class WeaponParticles {
  constructor(parent, effect, anchors, speed, isVisible, render, warning) {
    this.id = effect.id;
    this.parent = parent;
    this.group = new THREE.Group();
    this.from = new THREE.Vector3(...anchors[0]);
    this.to = new THREE.Vector3(...anchors[1]);
    this.velocity = new THREE.Vector3(...effect.velocity).clampLength(0, 20);
    this.colors = effect.colors.map(color => new THREE.Color(...color.map(value => value / 255)));
    this.scale = new THREE.Vector3(...effect.scale);
    this.speed = speed;
    this.phase = 100;
    this.fraction = 0;
    this.transform = new THREE.Object3D();
    this.color = new THREE.Color();
    // Playback uses a bounded browser lifetime. The original emitter's full update is not emulated.
    this.lifetime = 1.2;
    const count = Math.min(effect.count, effect.rate > 0 ? Math.max(1, Math.ceil(effect.rate * this.lifetime)) : effect.count);
    this.particles = Array.from({length: count}, (_, i) => ({
      age: this.lifetime * i / count,
      origin: new THREE.Vector3().lerpVectors(this.from, this.to, speed ? i / count : 1),
    }));
    this.meshes = effect.meshes.map(data => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(data.positions, 3));
      geometry.setAttribute('uv', new THREE.Float32BufferAttribute(data.uvs, 2));
      geometry.setIndex(data.indices);
      const material = new THREE.MeshBasicMaterial({transparent: true, depthWrite: false,
        side: THREE.DoubleSide, blending: THREE.AdditiveBlending, alphaTest: .01});
      // On a transparent canvas, opaque black TEX pixels can mask the CSS backdrop.
      material.onBeforeCompile = shader => {
        shader.fragmentShader = shader.fragmentShader.replace('#include <alphatest_fragment>',
          `diffuseColor.a *= max(max(diffuseColor.r, diffuseColor.g), diffuseColor.b);
           #include <alphatest_fragment>`);
      };
      const mesh = new THREE.InstancedMesh(geometry, material, count);
      mesh.frustumCulled = false;
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      if (data.texture !== null) {
        mesh.visible = false;
        new THREE.TextureLoader().load(`/api/texture/${data.texture}.png`, texture => {
          if (this.disposed) { texture.dispose(); return; }
          texture.colorSpace = THREE.SRGBColorSpace;
          texture.flipY = false;
          texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
          material.map = texture;
          material.needsUpdate = true;
          mesh.visible = true;
        }, undefined, () => { if (!this.disposed) warning('部分粒子贴图加载失败。'); });
      }
      this.group.add(mesh);
      return mesh;
    });
    parent.add(this.group);
    let previous = performance.now();
    const tick = time => {
      if (this.disposed) return;
      if (isVisible()) {
        this.update(Math.min(.1, Math.max(0, (time - previous) / 1000)));
        render();
      }
      previous = time;
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  /** Advance the native wrapping phase at a fixed preview tick, then move the rendered instances. */
  update(elapsed) {
    this.fraction += elapsed * 60;
    while (this.fraction >= 1) {
      this.phase -= this.speed;
      if (this.phase <= 0) this.phase = 100;
      if (this.phase > 100) this.phase = 1;
      this.fraction--;
    }
    for (const [index, particle] of this.particles.entries()) {
      particle.age += elapsed;
      if (particle.age >= this.lifetime) {
        particle.age %= this.lifetime;
        particle.origin.lerpVectors(this.from, this.to, this.phase / 100);
      }
      const life = particle.age / this.lifetime;
      this.transform.position.copy(particle.origin).addScaledVector(this.velocity, particle.age);
      this.transform.scale.copy(this.scale).multiplyScalar(1 - .35 * life);
      this.transform.updateMatrix();
      this.color.copy(this.colors[0]).lerp(this.colors[1], life).multiplyScalar(1 - life);
      for (const mesh of this.meshes) {
        mesh.setMatrixAt(index, this.transform.matrix);
        mesh.setColorAt(index, this.color);
      }
    }
    for (const mesh of this.meshes) {
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  /** Cancel playback and release every particle-owned GPU resource. */
  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.parent.remove(this.group);
    for (const mesh of this.meshes) {
      mesh.dispose();
      mesh.geometry.dispose();
      mesh.material.map?.dispose();
      mesh.material.dispose();
    }
  }
}
