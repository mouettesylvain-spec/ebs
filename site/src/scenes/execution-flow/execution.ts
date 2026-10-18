import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

// Token waypoints: centre of each stage box, then the hit or miss destination.
const STAGE_X = [80, 230, 380, 530];
const LANE_Y = 155;
const HIT = { x: 715, y: 60 };
const MISS = { x: 715, y: 240 };

export const buildExecution: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const tokens = $('.token');
  const hits = tokens.filter((t) => t.getAttribute('data-hit') === 'true');
  const misses = tokens.filter((t) => t.getAttribute('data-hit') !== 'true');
  const stages = $('.stage-box');

  tl.set(tokens, { cx: 80, cy: LANE_Y, opacity: 0 })
    .set($('.out-hit, .out-miss, .count-hit, .count-miss, .branch-hit, .branch-miss'), {
      opacity: 0.25,
    })
    .set(stages, { opacity: 0.4 });

  // flow.yaml → Plan → Keys → Cache: the stage lights up as the tokens pass.
  STAGE_X.forEach((x, i) => {
    const stage = stages[i];
    if (stage) tl.add(stage, { opacity: [0.4, 1], duration: 250 }, i === 0 ? 0 : '<');
    tl.add(
      tokens,
      {
        cx: x,
        cy: stagger(10, { start: LANE_Y - 20 }),
        opacity: 1,
        delay: stagger(60),
        duration: 650,
        ease: 'inOutQuad',
      },
      i === 0 ? 0 : '+=150',
    );
  });

  // The lookup splits them.
  tl.add($('.branch-hit, .out-hit'), { opacity: 1, duration: 300 }, '+=250')
    .add(hits, { cx: HIT.x, cy: HIT.y, delay: stagger(90), duration: 700, ease: 'inOutQuad' }, '<')
    .add($('.count-hit'), { opacity: 1, duration: 300 })
    .add($('.branch-miss, .out-miss'), { opacity: 1, duration: 300 }, '+=200')
    .add(misses, { cx: MISS.x, cy: MISS.y, delay: stagger(140), duration: 1100, ease: 'inOutQuad' }, '<')
    .add($('.count-miss'), { opacity: 1, duration: 300 })
    .add(tokens, { opacity: 0, duration: 400 }, '+=600');
  return tl;
};
