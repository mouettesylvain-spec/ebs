// Shared runtime of the animated scenes (src/scenes/*): every scene is an inline SVG, drawn in
// its final state, plus a builder that returns a paused anime.js timeline animating towards it.
//
// - Without JavaScript, or with prefers-reduced-motion, the reader sees the final diagram.
// - A scene starts when it scrolls into view, pauses when it leaves, and loops.
// - The replay button restarts it.
import { createTimeline, type Timeline } from 'animejs';

export interface SceneContext {
  root: HTMLElement;
  /** Elements of this scene only: `$('.packet')`. */
  $: (selector: string) => Element[];
  /** A paused, looping timeline with the site's easing and durations. */
  timeline: () => Timeline;
}

export type SceneBuilder = (ctx: SceneContext) => Timeline;

const reducedMotion = (): boolean =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function cssMs(root: HTMLElement, name: string, fallback: number): number {
  const value = getComputedStyle(root).getPropertyValue(name).trim();
  const ms = value.endsWith('ms') ? parseFloat(value) : parseFloat(value) * 1000;
  return Number.isFinite(ms) && ms > 0 ? ms : fallback;
}

function contextFor(root: HTMLElement): SceneContext {
  const duration = cssMs(root, '--ebs-duration-m', 450);
  return {
    root,
    $: (selector) => Array.from(root.querySelectorAll(selector)),
    timeline: () =>
      createTimeline({
        autoplay: false,
        loop: true,
        loopDelay: 2600,
        defaults: { duration, ease: 'outExpo' },
      }),
  };
}

function mount(root: HTMLElement, build: SceneBuilder): void {
  const tl = build(contextFor(root));
  const replay = root.querySelector<HTMLButtonElement>('[data-scene-replay]');

  if (reducedMotion()) {
    tl.seek(tl.duration);
    if (replay) replay.hidden = true;
    return;
  }

  tl.seek(0); // the builder's `set`s put the scene in its start state
  root.dataset.sceneState = 'ready';
  let visible = false;
  const observer = new IntersectionObserver(
    ([entry]) => {
      visible = entry?.isIntersecting ?? false;
      if (visible) tl.play();
      else tl.pause();
    },
    { threshold: 0.35 },
  );
  observer.observe(root);
  replay?.addEventListener('click', () => {
    tl.restart();
    if (!visible) tl.pause();
  });
}

/** Mount every `[data-scene="<name>"]` element of the page with `build`. */
export function mountScene(name: string, build: SceneBuilder): void {
  const start = (): void => {
    document
      .querySelectorAll<HTMLElement>(`[data-scene="${name}"]:not([data-scene-state])`)
      .forEach((root) => mount(root, build));
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
}
