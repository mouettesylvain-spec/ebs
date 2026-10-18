import { stagger } from 'animejs';
import type { SceneBuilder } from '../_lib/scene';

const OUTCOME_Y = [75, 145, 215];

export const buildRulePlugin: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const [packet] = $('.packet');
  const outcomes = $('.outcome');

  tl.set($('.expand, .template, .classify'), { opacity: 0.25 })
    .set($('.template .mono'), { opacity: 0 })
    .set(outcomes, { opacity: 0.25 })
    .set(packet ?? [], { opacity: 0 });

  tl.add($('.expand'), { opacity: [0.25, 1], duration: 400 })
    .add($('.template'), { opacity: [0.25, 1], duration: 400 })
    .add($('.template .mono'), { opacity: [0, 1], translateX: [-8, 0], delay: stagger(180), duration: 400 })
    .add($('.classify'), { opacity: [0.25, 1], duration: 400 }, '+=300');

  // Three runs, three verdicts: one per outcome, in turn.
  OUTCOME_Y.forEach((y, i) => {
    const outcome = outcomes[i];
    if (!packet || !outcome) return;
    tl.set(packet, { cx: 560, cy: 230, opacity: 1 })
      .add(packet, { cx: 630, cy: y, duration: 600, ease: 'inOutQuad' })
      .add(packet, { opacity: 0, duration: 150 })
      .add(outcome, { opacity: [0.25, 1], duration: 300 }, '<');
  });
  return tl;
};
