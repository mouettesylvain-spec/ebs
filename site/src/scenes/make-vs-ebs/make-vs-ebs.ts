import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

export const buildMakeVsEbs: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const lane = (id: string, selector: string): Element[] => $(`.lane-${id} ${selector}`);

  tl.set($('.overlay, .tag, .total'), { opacity: 0 }).set($('.edit'), { opacity: 0 });

  // The edit lands in both checkouts at once.
  tl.add($('.edit'), { opacity: [0, 1], r: [0, 6], duration: 500, ease: 'outBack' });

  // Make: the timestamp ripple reaches every step, one after the other.
  tl.add(lane('make', '.overlay'), { opacity: [0, 1], delay: stagger(380), duration: 300 }, '+=300')
    .add(lane('make', '.tag'), { opacity: [0, 1], delay: stagger(380), duration: 300 }, '<<')
    .add(lane('make', '.total'), { opacity: [0, 1], duration: 400 });

  // EBS: lint reruns, its report is byte-identical, the rest is a cache hit.
  const overlays = lane('ebs', '.overlay');
  const tags = lane('ebs', '.tag');
  tl.add(overlays.slice(0, 1), { opacity: [0, 1], duration: 300 }, '+=200')
    .add(tags.slice(0, 1), { opacity: [0, 1], duration: 300 }, '<<')
    .add(overlays.slice(1), { opacity: [0, 1], delay: stagger(120), duration: 300 }, '+=500')
    .add(tags.slice(1), { opacity: [0, 1], delay: stagger(120), duration: 300 }, '<<')
    .add(lane('ebs', '.total'), { opacity: [0, 1], duration: 400 });
  return tl;
};
