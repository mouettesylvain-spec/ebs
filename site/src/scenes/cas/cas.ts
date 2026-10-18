import type { SceneBuilder } from '../_lib/scene';

const HASHER = { cx: 300, cy: 178 };
const TMP = { cx: 550, cy: 95 };
const FILE_Y = [88, 178, 268]; // vertical centres of the three file cards
const ROW_Y = [211, 251];
const DIGESTS = ['sha256:3fa9c1…', 'sha256:b2075e…', 'sha256:3fa9c1…'];

export const buildCas: SceneBuilder = ({ $, timeline }) => {
  const tl = timeline();
  const [packet] = $('.packet');
  const [digest] = $('.digest');
  const rows = $('.row');
  const files = $('.file');
  const [fsync] = $('.fsync');
  const [dedupe] = $('.dedupe');
  const [ring] = $('.hasher-ring');
  if (!packet || !digest || !fsync || !dedupe || !ring) return tl;

  tl.set([...rows, digest, fsync, dedupe], { opacity: 0 })
    .set(files, { opacity: 0.35 })
    .set(packet, { opacity: 0 });

  FILE_Y.forEach((y, i) => {
    const file = files[i];
    if (!file) return;
    tl.add(file, { opacity: 1, duration: 300 })
      .set(packet, { cx: 200, cy: y, opacity: 1 })
      .add(packet, { cx: HASHER.cx, cy: HASHER.cy, duration: 700, ease: 'inOutQuad' })
      .add(ring, { r: [44, 52, 44], duration: 500, ease: 'inOutSine' }, '<<')
      .call(() => {
        digest.textContent = DIGESTS[i] ?? '';
      })
      .add(digest, { opacity: [0, 1], duration: 300 }, '<');

    const row = rows[i];
    if (row) {
      tl.add(packet, { cx: TMP.cx, cy: TMP.cy, duration: 700, ease: 'inOutQuad' })
        .add(fsync, { opacity: [0, 1, 1, 0], duration: 900, ease: 'linear' })
        .add(packet, { cx: 450, cy: ROW_Y[i] ?? 0, duration: 600, ease: 'inOutQuad' })
        .add(packet, { opacity: 0, duration: 200 })
        .add(row, { opacity: [0, 1], duration: 400 }, '<');
    } else {
      // Same bytes, same digest: the object exists, the upload is skipped.
      const first = rows[0];
      tl.add(packet, { cx: 450, cy: ROW_Y[0] ?? 0, duration: 800, ease: 'inOutQuad' })
        .add(packet, { opacity: 0, duration: 200 })
        .add(dedupe, { opacity: [0, 1], duration: 400 }, '<');
      if (first) tl.add(first, { opacity: [1, 0.4, 1], duration: 600 }, '<');
    }
  });
  return tl;
};
