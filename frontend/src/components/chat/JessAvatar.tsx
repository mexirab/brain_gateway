'use client';

import { useEffect, useRef, useState } from 'react';
import { onSpeechClip } from '@/lib/speech-clip-bus';
import { analyzeVisemes, decodeClip, sampleTrack, type VisemeTrack } from '@/lib/lipsync';
import { useReducedMotion } from '@/hooks/useReducedMotion';

/**
 * 3D VRM avatar for the chat page (ported from the jess-face Pi kiosk).
 * Renders on the viewer's own GPU with three.js + @pixiv/three-vrm, both
 * loaded lazily the first time the avatar is shown. Lip-sync comes from the
 * TTS clips the chat already plays (see speech-clip-bus + lipsync).
 *
 * The model is `public/avatar/jess.vrm` — gitignored, supplied by the
 * operator (e.g. exported from VRoid Studio). Missing → a hint, not a crash.
 */

export type AvatarMode = 'idle' | 'listening' | 'thinking' | 'speaking';

const MODEL_URL = '/avatar/jess.vrm';

const THEMES: Record<AvatarMode, { a: number; b: number }> = {
  idle: { a: 0x4fd8c7, b: 0x8b7cf6 },
  listening: { a: 0x60e7ff, b: 0x4fd8c7 },
  thinking: { a: 0x9382fa, b: 0x6084ff },
  speaking: { a: 0xff8d76, b: 0xffba7a },
};

// Expression smoothing rates (per second).
const EX_RATES: Record<string, number> = {
  aa: 18, ih: 18, ou: 18, blink: 26, happy: 8, relaxed: 5, surprised: 7,
};

type LoadState = 'loading' | 'ready' | 'missing' | 'error';

export default function JessAvatar({ mode }: { mode: AvatarMode }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const modeRef = useRef<AvatarMode>(mode);
  const reduced = useReducedMotion();
  const reducedRef = useRef(reduced);
  const [load, setLoad] = useState<LoadState>('loading');
  const [progress, setProgress] = useState(0);

  useEffect(() => { modeRef.current = mode; }, [mode]);
  useEffect(() => { reducedRef.current = reduced; }, [reduced]);

  useEffect(() => {
    let disposed = false;
    let raf = 0;
    let cleanup: (() => void) | null = null;

    // Current clip's viseme track + the element playing it.
    let track: VisemeTrack | null = null;
    let clipEl: HTMLAudioElement | null = null;
    let clipGen = 0;
    let ready = false; // model loaded — no point analysing clips before that
    const offClip = onSpeechClip((clip, el) => {
      if (!ready) return;
      const gen = ++clipGen;
      track = null;
      clipEl = el;
      decodeClip(clip)
        .then(({ samples, sampleRate }) => {
          if (gen === clipGen && !disposed) track = analyzeVisemes(samples, sampleRate);
        })
        .catch(() => { /* undecodable → mouth falls back to a gentle flap */ });
    });

    (async () => {
      let THREE: typeof import('three');
      let GLTFLoader: typeof import('three/examples/jsm/loaders/GLTFLoader.js').GLTFLoader;
      let VRMLoaderPlugin: typeof import('@pixiv/three-vrm').VRMLoaderPlugin;
      let VRMUtils: typeof import('@pixiv/three-vrm').VRMUtils;
      try {
        [THREE, { GLTFLoader }, { VRMLoaderPlugin, VRMUtils }] = await Promise.all([
          import('three'),
          import('three/examples/jsm/loaders/GLTFLoader.js'),
          import('@pixiv/three-vrm'),
        ]);
      } catch {
        if (!disposed) setLoad('error');
        return;
      }
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (disposed || !canvas || !wrap) return;

      let renderer: import('three').WebGLRenderer;
      try {
        // MSAA adds little on high-DPR phone screens and costs battery.
        const dpr = window.devicePixelRatio || 1;
        renderer = new THREE.WebGLRenderer({ canvas, antialias: dpr < 2, alpha: true, powerPreference: 'low-power' });
      } catch {
        setLoad('error');
        return;
      }
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(24, 1, 0.05, 20);
      scene.add(new THREE.HemisphereLight(0xbdd2ff, 0x1c2438, 0.85));
      const key = new THREE.DirectionalLight(0xffffff, 1.15);
      key.position.set(0.6, 1.9, 1.6);
      scene.add(key);
      const rim = new THREE.DirectionalLight(THEMES.idle.a, 0.9);
      rim.position.set(-0.8, 1.4, -1.2);
      scene.add(rim);

      // Soft glow halo behind the head.
      const glowCanvas = document.createElement('canvas');
      glowCanvas.width = glowCanvas.height = 256;
      const g = glowCanvas.getContext('2d');
      if (g) {
        const grad = g.createRadialGradient(128, 128, 0, 128, 128, 128);
        grad.addColorStop(0, 'rgba(255,255,255,0.9)');
        grad.addColorStop(0.4, 'rgba(255,255,255,0.28)');
        grad.addColorStop(1, 'rgba(255,255,255,0)');
        g.fillStyle = grad;
        g.fillRect(0, 0, 256, 256);
      }
      const glowTex = new THREE.CanvasTexture(glowCanvas);
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTex, color: THEMES.idle.a, blending: THREE.AdditiveBlending,
        depthWrite: false, transparent: true, opacity: 0.5,
      }));
      halo.scale.set(2.6, 2.6, 1);
      scene.add(halo);

      const lookTarget = new THREE.Object3D();
      scene.add(lookTarget);

      const resize = () => {
        const w = wrap.clientWidth || 1, h = wrap.clientHeight || 1;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
      };
      resize();
      const ro = new ResizeObserver(resize);
      ro.observe(wrap);

      // Mobile GPUs drop the context under memory pressure / long backgrounding.
      // three.js just stops drawing, so say so instead of showing a frozen face.
      const onLost = (e: Event) => { e.preventDefault(); if (!disposed) setLoad('error'); };
      canvas.addEventListener('webglcontextlost', onLost);

      cleanup = () => {
        ro.disconnect();
        canvas.removeEventListener('webglcontextlost', onLost);
        // deepDispose frees geometries, materials AND their textures (VRM
        // models carry several large ones); then release the GL context now
        // rather than whenever the browser garbage-collects it.
        VRMUtils.deepDispose(scene);
        glowTex.dispose();
        renderer.dispose();
        renderer.forceContextLoss();
      };

      // ---- load the model ----
      const loader = new GLTFLoader();
      loader.register((p) => new VRMLoaderPlugin(p));
      let gltf: import('three/examples/jsm/loaders/GLTFLoader.js').GLTF;
      try {
        gltf = await loader.loadAsync(MODEL_URL, (ev) => {
          if (ev.total && !disposed) setProgress(Math.round((ev.loaded / ev.total) * 100));
        });
      } catch (err) {
        if (disposed) return;
        // GLTFLoader surfaces a 404 as a fetch error mentioning the status.
        setLoad(/404|not found/i.test(String(err)) ? 'missing' : 'error');
        return;
      }
      if (disposed) {
        VRMUtils.deepDispose(gltf.scene); // toggled off mid-load
        return;
      }
      const vrm = gltf.userData.vrm as import('@pixiv/three-vrm').VRM | undefined;
      if (!vrm) { setLoad('error'); return; }
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.combineSkeletons(gltf.scene);
      if (vrm.meta?.metaVersion === '0') VRMUtils.rotateVRM0(vrm);
      scene.add(vrm.scene);

      const em = vrm.expressionManager ?? null;
      const avail = new Set(em ? em.expressions.map((e) => e.expressionName) : []);
      const bone = (name: Parameters<NonNullable<typeof vrm.humanoid>['getNormalizedBoneNode']>[0]) =>
        vrm.humanoid?.getNormalizedBoneNode(name) ?? null;

      // Relaxed A-pose instead of the rigging T-pose.
      const setZ = (name: Parameters<typeof bone>[0], z: number) => { const b = bone(name); if (b) b.rotation.z = z; };
      setZ('leftUpperArm', -1.15); setZ('rightUpperArm', 1.15);
      setZ('leftLowerArm', -0.1); setZ('rightLowerArm', 0.1);

      // Frame head and shoulders.
      vrm.scene.updateMatrixWorld(true);
      const headPos = new THREE.Vector3(0, 1.35, 0);
      bone('head')?.getWorldPosition(headPos);
      halo.position.set(headPos.x, headPos.y - 0.12, headPos.z - 0.5);
      lookTarget.position.set(0, headPos.y, headPos.z + 1.4);
      if (vrm.lookAt) vrm.lookAt.target = lookTarget;
      ready = true;
      setLoad('ready');

      // ---- animation ----
      const themeA = new THREE.Color(THEMES.idle.a);
      const targetA = new THREE.Color(THEMES.idle.a);
      const exVal: Record<string, number> = {};
      const exTgt: Record<string, number> = {};
      for (const k in EX_RATES) { exVal[k] = 0; exTgt[k] = 0; }
      let blinkT = 2.2 + Math.random() * 3.8;
      let blinkHold = 0;
      let gazeT = 3 + Math.random() * 5;
      let gazeHold = 0;
      const gazeOff = new THREE.Vector3();
      const base = new THREE.Vector3();
      let env = 0;
      let camDist = 1.2;
      let last = performance.now();
      let t = 0;

      const tick = () => {
        raf = requestAnimationFrame(tick);
        const now = performance.now();
        const m = modeRef.current;
        // 30 fps is plenty for breathing and blinks; full rate only while
        // speaking, when the mouth needs it. Saves battery on phones.
        if (m !== 'speaking' && now - last < 33) return;
        const dt = Math.min((now - last) / 1000, 0.1);
        last = now;
        t += dt;
        try {
          frame(dt, m);
        } catch (err) {
          // A frame that throws would throw every frame: stop and say so.
          cancelAnimationFrame(raf);
          console.warn('[avatar] render loop stopped', err);
          if (!disposed) setLoad('error');
        }
      };

      const frame = (dt: number, m: AvatarMode) => {
        const calm = reducedRef.current ? 0.25 : 1; // reduced motion: keep it subtle

        // Mouth: sample the current clip while its element is playing.
        const playing = clipEl !== null && !clipEl.paused && !clipEl.ended;
        let mouth = { aa: 0, ih: 0, ou: 0, env: 0 };
        if (m === 'speaking' && playing) {
          mouth = track
            ? sampleTrack(track, clipEl!.currentTime)
            // still decoding (first ~50 ms) → gentle flap so she isn't frozen
            : { aa: 0.25 + 0.2 * Math.sin(t * 14), ih: 0, ou: 0, env: 0.3 };
        }
        env = Math.max(env * 0.85, mouth.env);
        exTgt.aa = mouth.aa; exTgt.ih = mouth.ih; exTgt.ou = mouth.ou;

        // Blinks
        blinkT -= dt;
        if (blinkT <= 0) {
          exTgt.blink = 1; blinkHold = 0.095;
          blinkT = 2.3 + Math.random() * 4.3 - (Math.random() < 0.18 ? 2 : 0);
        }
        if (blinkHold > 0) { blinkHold -= dt; if (blinkHold <= 0) exTgt.blink = 0; }

        exTgt.happy = m === 'speaking' ? 0.13 : 0;
        exTgt.relaxed = m === 'thinking' ? 0.3 : 0;
        exTgt.surprised = m === 'listening' ? 0.22 : 0;
        for (const k in EX_RATES) {
          exVal[k] += (exTgt[k] - exVal[k]) * (1 - Math.exp(-EX_RATES[k] * dt));
          if (em && avail.has(k)) em.setValue(k, Math.max(0, Math.min(1, exVal[k])));
        }

        // Body: breathing, listening lean, thinking tilt, speaking nod.
        const spine = bone('spine'), chest = bone('chest'), neck = bone('neck'), head = bone('head'), hips = bone('hips');
        if (spine) spine.rotation.x = calm * 0.013 * Math.sin(t * 1.35) + (m === 'listening' ? 0.045 : 0);
        if (chest) chest.rotation.x = calm * 0.009 * Math.sin(t * 1.35 + 0.6);
        if (hips) hips.rotation.z = calm * 0.008 * Math.sin(t * 0.5);
        if (neck) neck.rotation.y = calm * 0.02 * Math.sin(t * 0.31 + 2);
        if (head) {
          let tiltZ = calm * 0.02 * Math.sin(t * 0.7 + 1);
          let nodX = 0;
          if (m === 'listening') tiltZ += 0.07;
          if (m === 'thinking') nodX = -0.06;
          if (m === 'speaking') nodX = -env * 0.07 + calm * 0.015 * Math.sin(t * 5.1);
          head.rotation.z = tiltZ;
          head.rotation.x = nodX;
        }

        // Gaze: wander when idle, up-left when thinking, at the viewer otherwise.
        gazeT -= dt;
        if (gazeHold > 0) { gazeHold -= dt; if (gazeHold <= 0) gazeOff.set(0, 0, 0); }
        if (m === 'idle') {
          if (gazeT <= 0 && !reducedRef.current) {
            gazeOff.set((Math.random() - 0.5) * 0.7, (Math.random() - 0.5) * 0.3, 0);
            gazeHold = 0.9 + Math.random() * 1.5;
            gazeT = 3.2 + Math.random() * 5.2;
          }
        } else if (m === 'thinking') {
          gazeOff.set(-0.45, 0.35, 0); gazeHold = 0;
        } else {
          gazeOff.set(calm * 0.05 * Math.sin(t * 0.4), calm * 0.02 * Math.sin(t * 0.9), 0); gazeHold = 0;
        }
        base.set(0, headPos.y, headPos.z + 1.4).add(gazeOff);
        lookTarget.position.lerp(base, 1 - Math.exp(-6 * dt));

        // Camera: slow drift + slight push-in while speaking.
        const push = m === 'speaking' ? 0.1 : m === 'listening' ? 0.06 : 0;
        const camTarget = 1.2 - calm * push; // reduced motion: almost no zoom
        camDist += (camTarget - camDist) * (1 - Math.exp(-2.5 * dt));
        camera.position.set(calm * 0.012 * Math.sin(t * 0.26), headPos.y + 0.05 + calm * 0.006 * Math.sin(t * 0.5), headPos.z + camDist);
        camera.lookAt(0, headPos.y + 0.02, headPos.z);

        // State colour on rim light + halo.
        targetA.setHex(THEMES[m].a);
        themeA.lerp(targetA, 1 - Math.exp(-4 * dt));
        rim.color.copy(themeA);
        rim.intensity = 0.8 + env * 1.4;
        halo.material.color.copy(themeA);
        halo.material.opacity = 0.38 + calm * 0.1 * Math.sin(t * 0.8) + env * 0.45;
        const hs = 2.6 + calm * 0.09 * Math.sin(t * 0.8) + env * 0.45;
        halo.scale.set(hs, hs, 1);

        vrm.update(dt);
        renderer.render(scene, camera);
      };
      tick();
    })();

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      offClip();
      cleanup?.();
    };
  }, []);

  return (
    <div ref={wrapRef} className="relative w-full h-full overflow-hidden rounded-xl bg-gradient-to-b from-surface-raised/40 to-transparent">
      <canvas ref={canvasRef} className="block w-full h-full" aria-hidden />
      <span className="sr-only">Jess avatar, {mode}</span>
      {load !== 'ready' && (
        <div role="status" className="absolute inset-0 flex items-center justify-center p-4 text-center text-xs text-content-muted">
          {load === 'loading' && <span>Waking Jess up{progress ? ` · ${progress}%` : '…'}</span>}
          {load === 'missing' && (
            <span>
              No avatar model yet. Put a VRM file (e.g. exported from VRoid Studio) at{' '}
              <code className="text-content-secondary">frontend/public/avatar/jess.vrm</code> and rebuild the frontend.
            </span>
          )}
          {load === 'error' && <span>The avatar couldn’t load on this device. Chat and voice still work.</span>}
        </div>
      )}
    </div>
  );
}
