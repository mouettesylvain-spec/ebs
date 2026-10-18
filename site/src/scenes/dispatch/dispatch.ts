import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

const SLURM = { x: 315, y: 160 };
const NODE_Y = [66, 156, 246]; // centre lines of the three nodes
const CAS = { x: 730, y: 95 };
const META = { x: 730, y: 235 };

export const buildDispatch: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const jobs = $('.job');
  const outs = $('.out');
  const nodes = $('.node');
  const lics = $('.lic');
  const [pending] = $('.pending');

  tl.set(jobs, { cx: 160, cy: 160, opacity: 0 })
    .set(outs, { opacity: 0 })
    .set($('.scratch, .scratch-text, .done'), { opacity: 0 })
    .set(nodes, { opacity: 0.45 })
    .set(pending ?? [], { opacity: 0 });

  // One sbatch call: the array arrives at the controller.
  tl.add(jobs, { opacity: 1, cx: SLURM.x, cy: stagger(12, { start: SLURM.y - 12 }), delay: stagger(120), duration: 700, ease: 'inOutQuad' });

  // Two licenses free: elements 0 and 1 start; element 2 waits in the queue.
  const start = (i: number, position?: string): void => {
    const job = jobs[i];
    const lic = lics[i % 2];
    const node = nodes[i];
    if (!job || !node) return;
    if (lic) tl.add(lic, { opacity: [1, 0.2], duration: 250 }, position);
    tl.add(job, { cx: 480, cy: NODE_Y[i] ?? 0, duration: 650, ease: 'inOutQuad' }, '<')
      .add(node, { opacity: 1, duration: 250 }, '<<')
      .add(node.querySelectorAll('.scratch, .scratch-text'), { opacity: [0, 1], duration: 300 })
      .add(job, { opacity: 0, duration: 200 }, '<');
  };
  const finish = (i: number): void => {
    const out = outs[i];
    const node = nodes[i];
    if (!out || !node) return;
    tl.set(out, { cx: 640, cy: NODE_Y[i] ?? 0, opacity: 1 })
      .add(out, { cx: CAS.x, cy: CAS.y, duration: 600, ease: 'inOutQuad' })
      .add(out, { cx: META.x, cy: META.y, duration: 500, ease: 'inOutQuad' })
      .add(out, { opacity: 0, duration: 150 })
      .add(node.querySelectorAll('.scratch, .scratch-text'), { opacity: 0, duration: 300 }, '<')
      .add(node.querySelectorAll('.done'), { opacity: [0, 1], duration: 300 }, '<');
  };

  start(0, '+=200');
  start(1, '<<+=150');
  if (pending) tl.add(pending, { opacity: [0, 1], duration: 300 });
  finish(0);
  const lic0 = lics[0];
  if (lic0) tl.add(lic0, { opacity: [0.2, 1], duration: 250 }); // license returned
  if (pending) tl.add(pending, { opacity: 0, duration: 250 }, '<');
  start(2);
  finish(1);
  finish(2);
  return tl;
};
