import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

export const buildRelease: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const pointer = $('.pointer');
  const pointerOld = $('.pointer-old');

  tl.set($('.build'), { opacity: 0 })
    .set($('.chosen-ring, .cut, .rel-new, .history'), { opacity: 0 })
    .set(pointer, { opacity: 0 })
    .set(pointerOld, { opacity: 1 });

  tl.add($('.build'), { opacity: [0, 1], translateY: [8, 0], delay: stagger(200), duration: 500 })
    // A team picks build 42 and publishes selected outputs by digest.
    .add($('.chosen-ring'), { opacity: [0, 1], duration: 400 }, '+=300')
    .add($('.cut'), { opacity: [0, 1], duration: 500 })
    .add($('.rel-new'), { opacity: [0, 1], translateY: [10, 0], duration: 600 })
    // `ebs channel set rtl-team/cpu stable 2026.09.3`: the pointer moves, history keeps both.
    .add(pointerOld, { opacity: [1, 0.25], duration: 500 }, '+=500')
    .add(pointer, { opacity: [0, 1], duration: 500 }, '<')
    .add($('.channel'), { opacity: [1, 0.5, 1], duration: 600 }, '<')
    .add($('.history'), { opacity: [0, 1], duration: 400 });
  return tl;
};
