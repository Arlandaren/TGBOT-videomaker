/**
 * Покадровый прогон движения графики — числа для `tests/test_overlay_motion.py`.
 *
 * Пробник ничего не считает сам: он импортирует `src/visuals/motion.mjs` — тот
 * же модуль, что рисует кадр в `Stage.tsx` и `Overlays.tsx`, — и печатает его
 * ответы по кадрам. В этом отличие от `card_probe.mjs`, который вынужден
 * держать копию кривых из TSX: здесь копии нет, и тест меряет ровно то, что
 * уходит в ролик.
 *
 *     node render/overlay_probe.mjs [fps]                  # для глаз
 *     node render/overlay_probe.mjs [fps] --json           # для теста
 *     node render/overlay_probe.mjs --json --plan <plan>   # графика из плана
 *
 * Без `--plan` прогоняется стенд: по элементу на каждую кривую входа (pop,
 * slide, fade), стикер с покачиванием, элемент, попросивший анимацию короче
 * восьми кадров, элемент, проживший меньше суммы входа и выхода, декор, карта
 * с меткой и три сцены. С `--plan` — всё, что лежит в `meta.visuals` плана
 * (fps, ширина и высота берутся из него же).
 *
 * Каждый ряд покрывает кадры от `first_frame` (за `PAD` кадров до начала) до
 * `PAD` кадров после конца: по ним тест проверяет и то, что вне своего окна
 * элемент не виден.
 */
import { readFileSync } from "node:fs";
import * as motion from "./src/visuals/motion.mjs";

const { activeScene, animFrames, frameOf, markAt, markDelay, overlayAt, pinAt, prng, stageAt } =
  motion;

/** Сколько кадров снять до начала и после конца. */
const PAD = 6;

const args = process.argv.slice(2);
const asJson = args.includes("--json");
const planAt = args.indexOf("--plan");
const planPath = planAt >= 0 ? args[planAt + 1] : null;
// Первый позиционный аргумент из одних цифр — fps стенда (значение `--plan` не в счёт).
const fpsArg = args.find((a, i) => /^\d+$/.test(a) && !(planAt >= 0 && i === planAt + 1));
if (planAt >= 0 && !planPath) {
  console.error("[overlay_probe] у --plan нет пути к плану");
  process.exit(2);
}

// --------------------------------------------------------------------------- стенд

/** Элемент плана со всеми полями: стенду хватает тех, что читает движение. */
const overlay = (fps, fields) => ({
  kind: "chip",
  in_frames: 10,
  out_frames: 8,
  rect: { x: 200, y: 400, width: 400, height: 100 },
  rotate: 0,
  anim: "pop",
  layer: "front",
  seed: 0,
  float_px: 0,
  marks: null,
  pin: null,
  ...fields,
  // Секунды — с точностью плана (три знака), на сетке кадров этого fps.
  start: Number((fields.from / fps).toFixed(3)),
  end: Number((fields.to / fps).toFixed(3)),
});

const bench = (fps) => {
  // Длины жизни — в секундах, чтобы стенд значил одно и то же на любом fps.
  const s = (seconds) => Math.round(seconds * fps);
  const overlays = [
    ["pop_tilted", overlay(fps, { from: s(0.5), to: s(2.5), rotate: -6, anim: "pop" })],
    ["pop_straight", overlay(fps, { from: s(0.5), to: s(2.5), anim: "pop" })],
    ["slide", overlay(fps, { from: s(1), to: s(3), anim: "slide" })],
    ["fade", overlay(fps, { from: s(1), to: s(3), anim: "fade" })],
    [
      "sticker_float",
      overlay(fps, {
        kind: "sticker",
        from: s(1),
        to: s(4),
        rotate: -8,
        seed: 1,
        float_px: 10,
        rect: { x: 400, y: 300, width: 160, height: 160 },
      }),
    ],
    [
      "photo",
      overlay(fps, {
        kind: "photo",
        from: s(1),
        to: s(3),
        rotate: 4,
        rect: { x: 500, y: 900, width: 440, height: 270 },
      }),
    ],
    // План попросил три кадра входа и два выхода: порог обязан сработать.
    ["asked_too_short", overlay(fps, { from: s(1), to: s(3), in_frames: 3, out_frames: 2 })],
    // Жизнь короче входа с выходом: кривые накладываются, покоя нет.
    ["tiny_life", overlay(fps, { from: s(1), to: s(1) + 12, in_frames: 10, out_frames: 8 })],
  ];
  const decor = overlay(fps, {
    kind: "decor",
    from: s(2),
    to: s(4),
    in_frames: 12,
    out_frames: 10,
    anim: "fade",
    seed: 3,
    glyph: "question",
    rect: { x: 60, y: 220, width: 960, height: 900 },
    marks: [
      { x: 160, y: 380, size: 150, rotate: -14, opacity: 0.55, phase: 0.0 },
      { x: 900, y: 330, size: 120, rotate: 12, opacity: 0.5, phase: 0.3 },
      { x: 130, y: 860, size: 110, rotate: 8, opacity: 0.45, phase: 0.6 },
      { x: 940, y: 800, size: 160, rotate: -10, opacity: 0.55, phase: 0.15 },
      { x: 540, y: 250, size: 100, rotate: 4, opacity: 0.4, phase: 0.8 },
      { x: 860, y: 1090, size: 90, rotate: -18, opacity: 0.4, phase: 0.45 },
    ],
  });
  const map = overlay(fps, {
    kind: "map",
    from: s(1),
    to: s(3),
    rotate: -3,
    seed: 7,
    rect: { x: 80, y: 910, width: 440, height: 280 },
    pin: { x: 250, y: 150 },
  });
  const scene = (layout, from, to, fields) => ({
    layout,
    start: Number((from / fps).toFixed(3)),
    end: Number((to / fps).toFixed(3)),
    in_frames: 12,
    out_frames: 12,
    radius: 44,
    tilt: 0,
    scale: 1,
    tx: 0,
    ty: 0,
    ...fields,
  });
  const scenes = [
    [
      "paper",
      scene("paper", s(5), s(8), {
        window: { x: 420, y: 420, width: 600, height: 760 },
        tilt: 3,
        scale: 0.62,
        tx: -11,
        ty: -137,
      }),
    ],
    [
      "card_top",
      scene("card_top", s(11), s(14), {
        window: { x: 60, y: 110, width: 960, height: 760 },
        radius: 56,
        scale: 0.889,
        ty: -313,
      }),
    ],
    [
      "phone",
      scene("phone", s(15), s(18), {
        window: { x: 190, y: 250, width: 700, height: 1244 },
        radius: 90,
        scale: 0.6481,
        in_frames: 9,
        out_frames: 15,
      }),
    ],
  ];
  return {
    fps,
    width: 1080,
    height: 1920,
    overlays: [...overlays, ["decor", decor], ["map", map]],
    scenes,
  };
};

/** Графика плана в том же виде, что стенд: пары «имя — объект». */
const fromPlan = (path) => {
  const plan = JSON.parse(readFileSync(path, "utf8"));
  const visuals = (plan.meta ?? {}).visuals ?? {};
  const scenes = (visuals.scenes ?? []).map((scene, i) => [`scene${i}_${scene.layout}`, scene]);
  const overlays = (visuals.overlays ?? []).map((o, i) => [`overlay${i}_${o.kind}`, o]);
  scenes.forEach(([name, scene]) => {
    if (scene.heading) overlays.push([`${name}_heading`, scene.heading]);
    (scene.items ?? []).forEach((item, j) => {
      overlays.push([`${name}_item${j}_${item.kind}`, item]);
    });
  });
  return { fps: plan.fps, width: plan.width, height: plan.height, overlays, scenes };
};

// --------------------------------------------------------------------------- прогон

const input = planPath ? fromPlan(planPath) : bench(Number(fpsArg || 30));
const { fps, width, height } = input;

/** Ряды значений по кадрам: `sample(frame)` → объект, ключи становятся рядами. */
const series = (f0, f1, sample) => {
  const rows = {};
  for (let frame = f0 - PAD; frame <= f1 + PAD; frame++) {
    for (const [key, value] of Object.entries(sample(frame))) (rows[key] ??= []).push(value);
  }
  return { first_frame: f0 - PAD, ...rows };
};

const report = {
  fps,
  width,
  height,
  pad: PAD,
  min_anim_frames: motion.MIN_ANIM_FRAMES,
  constants: Object.fromEntries(
    Object.entries(motion).filter(([, value]) => typeof value === "number"),
  ),
  overlays: [],
  marks: [],
  pins: [],
  scenes: [],
  prng: {},
};

for (const [name, o] of input.overlays) {
  const f0 = frameOf(o.start, fps);
  const f1 = frameOf(o.end, fps);
  const head = {
    name,
    kind: o.kind,
    anim: o.anim,
    f0,
    f1,
    in_frames: animFrames(o.in_frames),
    out_frames: animFrames(o.out_frames),
    asked_in: o.in_frames,
    asked_out: o.out_frames,
  };
  if (o.kind === "decor") {
    (o.marks ?? []).forEach((mark, index) => {
      report.marks.push({
        ...head,
        name: `${name}/${index}`,
        decor: name,
        index,
        delay: markDelay(o, index, fps),
        base_opacity: mark.opacity,
        rest_rotate: mark.rotate,
        size: mark.size,
        ...series(f0, f1, (frame) => markAt(o, index, frame, fps)),
      });
    });
    continue;
  }
  report.overlays.push({
    ...head,
    rest_rotate: o.rotate,
    float_px: o.float_px ?? 0,
    height: o.rect.height,
    ...series(f0, f1, (frame) => overlayAt(o, frame, fps)),
  });
  if (o.kind === "map" && o.pin) {
    report.pins.push({ ...head, ...series(f0, f1, (frame) => pinAt(o, frame, fps)) });
  }
}

const sceneList = input.scenes.map(([, scene]) => scene);
for (const [name, scene] of input.scenes) {
  const f0 = frameOf(scene.start, fps);
  const f1 = frameOf(scene.end, fps);
  report.scenes.push({
    name,
    layout: scene.layout,
    f0,
    f1,
    in_frames: scene.in_frames,
    out_frames: scene.out_frames,
    rest: {
      ...scene.window,
      radius: scene.radius,
      tilt: scene.tilt,
      scale: scene.scale,
      tx: scene.tx,
      ty: scene.ty,
    },
    ...series(f0, f1, (frame) => ({
      ...stageAt(scene, frame, fps, width, height),
      // Какую сцену рендерер считает идущей на этом кадре (номер или -1).
      active: activeScene(sceneList, frame, fps),
    })),
  });
}

for (const seed of [0, 7, 8]) {
  const next = prng(seed);
  report.prng[seed] = Array.from({ length: 8 }, () => next());
}

if (asJson) {
  console.log(JSON.stringify(report));
} else {
  const fmt = (values, digits = 3) => values.map((v) => Number(v).toFixed(digits)).join(" ");
  console.log(`графика @${fps}fps, кадр ${width}x${height}, по ${PAD} кадров до и после`);
  for (const o of report.overlays) {
    const peak = Math.max(...o.scale);
    console.log(
      `\n${o.name} (${o.kind}, ${o.anim}): кадры ${o.f0}..${o.f1}, вход ${o.in_frames} ` +
        `(просили ${o.asked_in}), выход ${o.out_frames} (просили ${o.asked_out}), ` +
        `пик масштаба ${peak.toFixed(4)}`,
    );
    const head = o.in_frames + PAD + 3;
    const tail = o.out_frames + PAD + 3;
    console.log("   вход  альфа:  ", fmt(o.opacity.slice(0, head)));
    console.log("   вход  масштаб:", fmt(o.scale.slice(0, head)));
    console.log("   вход  dy:     ", fmt(o.dy.slice(0, head), 1));
    console.log("   вход  угол:   ", fmt(o.rotate.slice(0, head), 1));
    console.log("   выход альфа:  ", fmt(o.opacity.slice(-tail)));
    console.log("   выход масштаб:", fmt(o.scale.slice(-tail)));
  }
  for (const m of report.marks) {
    const seen = m.visible.indexOf(true);
    console.log(
      `\n${m.name}: задержка ${m.delay}, первый видимый кадр ${m.first_frame + seen}, ` +
        `альфа до ${Math.max(...m.opacity).toFixed(3)} из ${m.base_opacity}`,
    );
  }
  for (const p of report.pins) {
    const seen = p.visible.indexOf(true);
    console.log(`\nметка ${p.name}: падает с кадра ${p.first_frame + seen}`);
    console.log("   dy:  ", fmt(p.dy.slice(seen - 1, seen + 14), 1));
    console.log("   тень:", fmt(p.shadow.slice(seen - 1, seen + 14), 2));
  }
  for (const s of report.scenes) {
    console.log(
      `\nсцена ${s.name}: кадры ${s.f0}..${s.f1}, вход ${s.in_frames}, выход ${s.out_frames}`,
    );
    console.log("   вход  p:", fmt(s.p.slice(0, s.in_frames + PAD + 2)));
    console.log("   выход p:", fmt(s.p.slice(-(s.out_frames + PAD + 2))));
  }
}
