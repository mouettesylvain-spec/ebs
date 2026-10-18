import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

export const buildToolchain: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();

  tl.set($('.file, .env'), { opacity: 0 })
    .set($('.patch, .id-new, .key-miss'), { opacity: 0 })
    .set($('.id-old, .key-hit, .fp, .key'), { opacity: 0 });

  // Fingerprint the tree, capture the env, hash them into the id.
  tl.add($('.file'), { opacity: [0, 1], translateX: [-10, 0], delay: stagger(140), duration: 400 })
    .add($('.env'), { opacity: [0, 1], translateX: [-10, 0], delay: stagger(140), duration: 400 }, '-=200')
    .add($('.fp'), { opacity: [0, 1], duration: 400 })
    .add($('.id-old'), { opacity: [0, 1], duration: 400 })
    .add($('.key'), { opacity: [0, 1], duration: 400 })
    .add($('.key-hit'), { opacity: [0, 1], duration: 400 })
    // Someone patches a library in place: same module name, new bits.
    .add($('.patch'), { opacity: [0, 1], duration: 400 }, '+=900')
    .add($('.id-old, .key-hit'), { opacity: 0, duration: 300 }, '+=300')
    .add($('.id-new'), { opacity: [0, 1], duration: 400 })
    .add($('.key-miss'), { opacity: [0, 1], duration: 400 }, '+=150');
  return tl;
};
