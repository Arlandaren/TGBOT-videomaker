/**
 * Движение графики: чистые функции кадра. Ни React, ни импортов, ни случайности.
 *
 * Модуль один на двоих — его импортируют и компоненты (`Stage.tsx`,
 * `Overlays.tsx`), и пробник (`render/overlay_probe.mjs`). Поэтому тест
 * (`tests/test_overlay_motion.py`) меряет ровно ту арифметику, что уходит в
 * кадр: копии кривых, как у `card_probe.mjs`, здесь нет, и разойтись ей не с чем.
 *
 * Что здесь закон, а что вкус:
 *
 * - Сцена (`stageAt`) — формулы из шапки `src/core/overlay_models.py`, слово в
 *   слово. По ним Python пересчитывает трек лица, и любая «улучшенная» кривая
 *   здесь развела бы окно в кадре с окном, которое он посчитал.
 * - Элемент (`overlayAt`, `markAt`, `pinAt`) — кривые наши, но с требованиями:
 *   не короче восьми кадров, перелёт масштаба не больше 4 % и ровно один
 *   разворот, покой ровно 1.0, прозрачность монотонна, вне [start, end] ничего
 *   не видно.
 *
 * Времени и случайности нет: `Math.random` и `Date` запрещены, перерендер
 * сохранённого листа обязан дать те же кадры. Всё, что похоже на случайность
 * (фаза покачивания, улицы карты), выводится из `seed` плана.
 */

/** Короче восьми кадров переход читается как мигание (`style.ts`, тот же закон). */
export const MIN_ANIM_FRAMES = 8;

/** Вход «pop»: с какого масштаба, на сколько перелетает и где вершина (доля окна). */
export const POP_FROM = 0.6;
export const POP_OVERSHOOT = 0.03;
export const POP_PEAK_AT = 0.6;
/** Альфа «pop» доезжает раньше масштаба: рост обязан идти на уже видимом элементе. */
export const POP_FADE = 0.4;
/** До какого масштаба элемент сжимается, уходя. */
export const POP_OUT = 0.86;
/** Насколько элемент довёрнут в начале входа, градусы: стикер, карточка, плашка. */
export const SPIN_DEG = { sticker: 16, photo: 7, map: 7, chip: 5 };

/** Выезд «slide»: ход в долях высоты элемента и его границы в пикселях. */
export const SLIDE_RATIO = 0.5;
export const SLIDE_MIN_PX = 24;
export const SLIDE_MAX_PX = 120;

/** Покачивание стикера в покое: период, с. Медленно — иначе это дрожь. */
export const FLOAT_PERIOD_S = 2.6;
export const FLOAT_SWAY_DEG = 1.6;

/** Декор: задержка между знаками, с, и с какого масштаба знак проступает. */
export const MARK_STAGGER_S = 0.11;
export const MARK_FROM = 0.78;
export const MARK_OUT = 0.9;
export const MARK_PERIOD_S = 3.4;
/** Амплитуда покачивания и снос вверх за секунду — в долях кегля знака. */
export const MARK_FLOAT_RATIO = 0.055;
export const MARK_DRIFT_RATIO = 0.045;
export const MARK_SWAY_DEG = 3.5;

/** Метка карты: сколько падает, с, с какой высоты (доля высоты карты), отскок. */
export const PIN_DROP_S = 0.42;
export const PIN_DROP_RATIO = 0.5;
export const PIN_FALL = 0.72;
export const PIN_HOP = 0.1;

const TAU = Math.PI * 2;

const clamp01 = (t) => (t >= 1 ? 1 : t > 0 ? t : 0);

/** Та самая кривая сцены. Вторая копия — `overlay_models.smoothstep`. */
export const smoothstep = (t) => {
  const x = clamp01(t);
  return x * x * (3 - 2 * x);
};

/** Быстрый старт, длинное торможение — приезд на место. */
const easeOut = (t) => {
  const x = 1 - clamp01(t);
  return 1 - x * x * x;
};

/** Секунды плана → номер кадра. Секунды уже лежат на сетке, округление — от хвостов. */
export const frameOf = (seconds, fps) => Math.round(Number(seconds) * fps);

/**
 * Длительность анимации элемента в кадрах: число плана, но не меньше порога.
 * У сцены порога НЕТ (см. `sceneProgress`): её кривая — контракт с Python.
 */
export const animFrames = (frames) =>
  Math.max(MIN_ANIM_FRAMES, Math.round(Number(frames) || 0));

/** Дробная часть: фаза из зерна. Золотое сечение разводит соседние зёрна далеко. */
const phaseOf = (seed) => {
  const v = (Number(seed) || 0) * 0.6180339887498949;
  return v - Math.floor(v);
};

/**
 * Маленький генератор с зерном (mulberry32) — для карты и всего, что должно
 * выглядеть случайным, оставаясь одним и тем же от рендера к рендеру.
 */
export const prng = (seed) => {
  let a = ((Math.floor(Number(seed) || 0) | 0) ^ 0x9e3779b9) >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
};

// --------------------------------------------------------------------------- сцена

/**
 * Ход сцены `p` на кадре, 0..1 — формула контракта:
 *
 *     вход   p = smoothstep((f - f0) / in_frames)
 *     покой  p = 1
 *     выход  p = smoothstep((f1 - f) / out_frames)   — выход КОНЧАЕТСЯ в `end`
 *
 * Минимум двух ветвей — это они же: в покое обе равны единице. Порог в восемь
 * кадров сюда не ставится намеренно: Python считает окно говорящего по числам
 * плана, и «исправленная» здесь длительность развела бы трек лица с кадром.
 */
export const sceneProgress = (scene, frame, fps) => {
  if (!scene) return 0;
  const f0 = frameOf(scene.start, fps);
  const f1 = frameOf(scene.end, fps);
  if (!(frame > f0 && frame < f1)) return 0;
  const enter = smoothstep((frame - f0) / Math.max(1, Number(scene.in_frames) || 0));
  const leave = smoothstep((f1 - frame) / Math.max(1, Number(scene.out_frames) || 0));
  return Math.min(enter, leave);
};

/**
 * Номер сцены, идущей на кадре, или -1. Сцены не пересекаются (это держит
 * валидатор листа); если всё же пересеклись — берётся первая, а не смесь двух.
 */
export const activeScene = (scenes, frame, fps) => {
  if (!Array.isArray(scenes)) return -1;
  for (let i = 0; i < scenes.length; i++) {
    const scene = scenes[i];
    if (frame >= frameOf(scene.start, fps) && frame <= frameOf(scene.end, fps)) return i;
  }
  return -1;
};

/**
 * Окно говорящего и видео внутри него на кадре — линейно по `p` от полного
 * кадра к покою сцены. Без сцены (`null`) — нейтральное состояние: окно во
 * весь кадр, видео без сдвига и масштаба.
 *
 * `scale`, `tx`, `ty` — уже текущие значения: блок W×H рисуется с
 * `transform-origin: 0 0; transform: translate(tx px, ty px) scale(scale)`.
 */
export const stageAt = (scene, frame, fps, W, H) => {
  const p = sceneProgress(scene, frame, fps);
  if (!scene || p === 0) {
    return { p: 0, x: 0, y: 0, width: W, height: H, radius: 0, tilt: 0, scale: 1, tx: 0, ty: 0 };
  }
  const win = scene.window;
  if (p === 1) {
    // Покой — числа плана как есть, а не `W + 1*(w - W)`: у дробных значений
    // такая сумма расходится с исходным числом в последнем знаке.
    return {
      p,
      x: win.x,
      y: win.y,
      width: win.width,
      height: win.height,
      radius: scene.radius,
      tilt: scene.tilt,
      scale: scene.scale,
      tx: scene.tx,
      ty: scene.ty,
    };
  }
  return {
    p,
    x: p * win.x,
    y: p * win.y,
    width: W + p * (win.width - W),
    height: H + p * (win.height - H),
    radius: p * scene.radius,
    tilt: p * scene.tilt,
    scale: 1 + p * (scene.scale - 1),
    tx: p * scene.tx,
    ty: p * scene.ty,
  };
};

// --------------------------------------------------------------------------- элемент

/**
 * Масштаб входа «pop» по ходу окна `t` (0..1): подъём до вершины и посадка.
 *
 * Склеен из двух кусков, а не взят пружиной: у пружины разворотов столько,
 * сколько колебаний, и перелёт — следствие жёсткости, а не заказанное число.
 * Здесь подъём монотонный (четверть синуса, скорость на вершине — ноль),
 * спуск — `smoothstep` (скорость ноль на обоих концах), поэтому разворот ровно
 * один, на вершине нет угла, а посадка — ровно 1.0, без хвоста округлений.
 */
const popScale = (t) => {
  if (t >= 1) return 1;
  if (t <= 0) return POP_FROM;
  const peak = 1 + POP_OVERSHOOT;
  if (t < POP_PEAK_AT) {
    return POP_FROM + (peak - POP_FROM) * Math.sin((t / POP_PEAK_AT) * (Math.PI / 2));
  }
  return 1 + POP_OVERSHOOT * (1 - smoothstep((t - POP_PEAK_AT) / (1 - POP_PEAK_AT)));
};

/** Ход выезда «slide» в пикселях — от высоты элемента, в разумных границах. */
const slidePx = (overlay) => {
  const height = Number(overlay.rect && overlay.rect.height) || 0;
  return Math.min(SLIDE_MAX_PX, Math.max(SLIDE_MIN_PX, height * SLIDE_RATIO));
};

/** Въезд сбоку: ход от ширины элемента, в разумных границах. */
export const SIDE_RATIO = 0.9;
export const SIDE_MIN_PX = 60;
export const SIDE_MAX_PX = 320;
export const SIDE_FADE = 0.5;
const sidePx = (overlay) => {
  const width = Number(overlay.rect && overlay.rect.width) || 0;
  return Math.min(SIDE_MAX_PX, Math.max(SIDE_MIN_PX, width * SIDE_RATIO));
};

/** Падение сверху: высота от высоты элемента, доля окна на падение и отскок. */
export const DROP_RATIO = 1.2;
export const DROP_MIN_PX = 80;
export const DROP_MAX_PX = 260;
export const DROP_FADE = 0.3;
export const DROP_FALL = 0.7;
export const DROP_HOP = 0.08;
const dropPx = (overlay) => {
  const height = Number(overlay.rect && overlay.rect.height) || 0;
  return Math.min(DROP_MAX_PX, Math.max(DROP_MIN_PX, height * DROP_RATIO));
};
/** Высота над местом по ходу окна: 1 → 0 с разгоном, потом один отскок. */
const dropLift = (t) => {
  if (t >= 1) return 0;
  if (t <= 0) return 1;
  if (t < DROP_FALL) {
    const u = t / DROP_FALL;
    return 1 - u * u;
  }
  return DROP_HOP * Math.sin(Math.PI * ((t - DROP_FALL) / (1 - DROP_FALL)));
};

/** Штамп: с какого масштаба садится и за какую долю окна проявляется. */
export const STAMP_FROM = 1.6;
export const STAMP_FADE = 0.35;

/** Маркер: за какую долю окна проявляется коробка. */
export const WIPE_FADE = 0.2;

/**
 * Сколько полосы маркера уже проведено, 0..1: растёт слева направо за окно
 * входа и стоит до конца. У элемента с другим входом полоса проведена сразу.
 */
export const wipeAt = (overlay, frame, fps) => {
  if (overlay.anim !== "wipe") return 1;
  const f0 = frameOf(overlay.start, fps);
  return easeOut((frame - f0) / animFrames(overlay.in_frames));
};

/**
 * Счётчик плашки (`overlay.count`): число на кадре или `null`, если счёта нет
 * или он уже добежал. Число цифр не меняется — счёт идёт от 10…0, а не от
 * нуля, — поэтому ширина текста стоит и коробка плана остаётся верной.
 */
export const countAt = (overlay, frame, fps) => {
  const count = overlay.count;
  if (!count || !(Number(count.value) > 0)) return null;
  const f0 = frameOf(overlay.start, fps);
  const frames = animFrames(count.frames);
  if (frame >= f0 + frames) return null;
  const value = Math.floor(Number(count.value));
  const floor = Math.pow(10, String(value).length - 1);
  const t = (frame - f0) / frames;
  return Math.max(floor, Math.min(value, Math.round(floor + (value - floor) * easeOut(t))));
};

/**
 * В какую сторону и на сколько элемент довёрнут в начале входа.
 *
 * Сторона — та же, куда он наклонён в покое (карточку «докручивают» до места),
 * а у стоящего прямо — по чётности зерна. Плашка без наклона не крутится
 * вовсе: ровная капсула, вращающаяся на входе, читается как сбой.
 */
const spinOf = (overlay) => {
  const amount = SPIN_DEG[overlay.kind] ?? 0;
  if (!amount) return 0;
  const rest = Number(overlay.rotate) || 0;
  if (overlay.kind === "chip" && rest === 0) return 0;
  if (rest !== 0) return rest > 0 ? amount : -amount;
  return (Math.floor(Number(overlay.seed) || 0) & 1) === 0 ? -amount : amount;
};

/**
 * Состояние элемента на кадре: видимость, прозрачность, масштаб, сдвиг в
 * пикселях и ПОЛНЫЙ угол в градусах (наклон покоя уже внутри).
 *
 * Вход идёт `in_frames` кадров от `start`, выход — `out_frames` кадров и
 * КОНЧАЕТСЯ в `end`. На самих кадрах `start` и `end` альфа — ноль: первый
 * видимый кадр — следующий за `start`, последний — предыдущий перед `end`.
 *
 * - `pop` — масштаб 0.6 → 1.03 → 1, альфа доезжает за первые 40 % окна,
 *   стикер и карточка при этом доворачиваются до своего наклона;
 * - `slide` — выезд снизу без масштаба, уход дальше вверх;
 * - `fade` — одна прозрачность.
 *
 * Вход и выход перемножаются, а не выбираются: у элемента, прожившего меньше
 * суммы двух окон, кривые накладываются, и обе обязаны остаться непрерывными.
 */
export const overlayAt = (overlay, frame, fps) => {
  const rest = Number(overlay.rotate) || 0;
  const f0 = frameOf(overlay.start, fps);
  const f1 = frameOf(overlay.end, fps);
  if (!(frame > f0 && frame < f1)) {
    return { visible: false, opacity: 0, scale: 1, dx: 0, dy: 0, rotate: rest };
  }
  const t = clamp01((frame - f0) / animFrames(overlay.in_frames));
  const q = clamp01((f1 - frame) / animFrames(overlay.out_frames));
  const gone = 1 - q;

  let opacity = Math.min(smoothstep(t), smoothstep(q));
  let scale = 1;
  let dx = 0;
  let dy = 0;
  let rotate = rest;

  if (overlay.anim === "pop") {
    opacity = Math.min(smoothstep(t / POP_FADE), smoothstep(q));
    scale = popScale(t) * (1 - (1 - POP_OUT) * gone * gone);
    rotate += spinOf(overlay) * (1 - easeOut(t));
  } else if (overlay.anim === "slide") {
    const px = slidePx(overlay);
    opacity = Math.min(smoothstep(t / 0.6), smoothstep(q));
    dy = px * (1 - easeOut(t)) - px * 0.5 * gone * gone;
  } else if (overlay.anim === "from_left" || overlay.anim === "from_right") {
    // Въезд сбоку: элемент приезжает со своей стороны кадра и туда же уходит.
    const side = overlay.anim === "from_left" ? -1 : 1;
    const px = sidePx(overlay);
    opacity = Math.min(smoothstep(t / SIDE_FADE), smoothstep(q));
    dx = side * px * (1 - easeOut(t)) + side * px * 0.4 * gone * gone;
    rotate += spinOf(overlay) * 0.5 * (1 - easeOut(t));
  } else if (overlay.anim === "drop") {
    // Падение сверху: разгон к месту и один короткий отскок, как у метки карты.
    const px = dropPx(overlay);
    opacity = Math.min(smoothstep(t / DROP_FADE), smoothstep(q));
    dy = -px * dropLift(t) - px * 0.25 * gone * gone;
  } else if (overlay.anim === "stamp") {
    // Штамп: элемент крупнее своего размера и садится на место без перелёта.
    opacity = Math.min(smoothstep(t / STAMP_FADE), smoothstep(q));
    scale = (1 + (STAMP_FROM - 1) * (1 - easeOut(t))) * (1 - (1 - POP_OUT) * gone * gone);
  } else if (overlay.anim === "wipe") {
    // Маркер: коробка стоит, полосу проводит `wipeAt` — здесь только альфа.
    opacity = Math.min(smoothstep(t / WIPE_FADE), smoothstep(q));
  }

  // Покачивание в покое. Огибающая входа — чтобы элемент приезжал на своё
  // место, а не в точку, уже сдвинутую качанием.
  const amplitude = Number(overlay.float_px) || 0;
  if (amplitude > 0) {
    const seconds = (frame - f0) / fps;
    const phase = phaseOf(overlay.seed);
    const envelope = smoothstep(t);
    // Три оси качаются с разными периодами: с одним на всех стикер ходил бы
    // по одной и той же дуге, как маятник.
    const swing = (period, shift) => envelope * Math.sin(TAU * (seconds / period + shift));
    dy += amplitude * swing(FLOAT_PERIOD_S, phase);
    dx += amplitude * 0.35 * swing(FLOAT_PERIOD_S * 1.37, phase * 1.7);
    rotate += FLOAT_SWAY_DEG * swing(FLOAT_PERIOD_S * 1.2, phase + 0.25);
  }

  return { visible: opacity > 0, opacity, scale, dx, dy, rotate };
};

// --------------------------------------------------------------------------- декор

/**
 * Через сколько кадров после начала декора входит знак номер `index`.
 *
 * Шаг — `MARK_STAGGER_S`, но не больше, чем позволяет жизнь декора: последний
 * знак обязан успеть войти до начала общего ухода, иначе он появлялся бы уже
 * растворяясь.
 */
export const markDelay = (overlay, index, fps) => {
  const count = Array.isArray(overlay.marks) ? overlay.marks.length : 0;
  if (count < 2 || index <= 0) return 0;
  const life = frameOf(overlay.end, fps) - frameOf(overlay.start, fps);
  const room = life - animFrames(overlay.in_frames) - animFrames(overlay.out_frames);
  const fits = Math.floor(Math.max(0, room) / (count - 1));
  return Math.min(Math.round(MARK_STAGGER_S * fps), fits) * index;
};

/**
 * Состояние одного знака декора: проступает со своей задержкой, плывёт вверх и
 * покачивается со своей фазой, уходят все знаки вместе, заканчивая в `end`.
 *
 * `opacity` — уже с собственной прозрачностью знака (`mark.opacity`), `rotate`
 * — полный угол. `dx`/`dy` — сдвиг от центра знака (`mark.x`, `mark.y`).
 */
export const markAt = (overlay, index, frame, fps) => {
  const mark = (overlay.marks || [])[index];
  const rest = Number(mark && mark.rotate) || 0;
  const f0 = frameOf(overlay.start, fps);
  const f1 = frameOf(overlay.end, fps);
  const begin = f0 + markDelay(overlay, index, fps);
  if (!mark || !(frame > begin && frame < f1)) {
    return { visible: false, opacity: 0, scale: 1, dx: 0, dy: 0, rotate: rest };
  }
  const t = clamp01((frame - begin) / animFrames(overlay.in_frames));
  const q = clamp01((f1 - frame) / animFrames(overlay.out_frames));
  const gone = 1 - q;
  const fade = Math.min(smoothstep(t), smoothstep(q));
  const opacity = (Number(mark.opacity) || 0) * fade;
  // Записано от единицы вниз: в покое оба вычитаемых — точный ноль.
  const scale = (1 - (1 - MARK_FROM) * (1 - easeOut(t))) * (1 - (1 - MARK_OUT) * gone * gone);

  // Время общее для всех знаков (от начала декора), фаза у каждого своя:
  // тогда знаки не дышат в унисон, но и не дёргаются при своём появлении.
  const seconds = (frame - f0) / fps;
  const size = Number(mark.size) || 0;
  const phase = Number(mark.phase) || 0;
  const wave = size * MARK_FLOAT_RATIO;
  const lived = (frame - begin) / fps;
  return {
    visible: opacity > 0,
    opacity,
    scale,
    dx: wave * 0.6 * Math.cos(TAU * (seconds / (MARK_PERIOD_S * 1.3) + phase)),
    dy: wave * Math.sin(TAU * (seconds / MARK_PERIOD_S + phase)) - size * MARK_DRIFT_RATIO * lived,
    rotate: rest + MARK_SWAY_DEG * Math.sin(TAU * (seconds / (MARK_PERIOD_S * 0.9) + phase)),
  };
};

// --------------------------------------------------------------------------- метка карты

/**
 * Метка карты: падает на своё место, когда карточка уже встала, один раз
 * подпрыгивает и стоит. `dy` — сдвиг острия вверх от места (отрицательный),
 * `shadow` — насколько проявилась тень под остриём (0..1).
 *
 * Падение начинается с концом входа карточки, но не позже, чем нужно, чтобы
 * успеть приземлиться до её ухода.
 */
export const pinAt = (overlay, frame, fps) => {
  const f0 = frameOf(overlay.start, fps);
  const f1 = frameOf(overlay.end, fps);
  const frames = Math.max(MIN_ANIM_FRAMES, Math.round(PIN_DROP_S * fps));
  const enter = animFrames(overlay.in_frames);
  const begin = Math.max(f0, Math.min(f0 + enter, f1 - animFrames(overlay.out_frames) - frames));
  if (!(frame > begin && frame < f1)) return { visible: false, opacity: 0, dy: 0, shadow: 0 };
  const t = clamp01((frame - begin) / frames);
  const drop = (Number(overlay.rect && overlay.rect.height) || 0) * PIN_DROP_RATIO;
  let lift = 0;
  let shadow = 1;
  if (t < PIN_FALL) {
    // Падение с ускорением: метка разгоняется к земле, а не тормозит перед ней.
    const u = t / PIN_FALL;
    lift = 1 - u * u;
    shadow = u * u;
  } else if (t < 1) {
    const hop = Math.sin(Math.PI * ((t - PIN_FALL) / (1 - PIN_FALL)));
    lift = PIN_HOP * hop;
    shadow = 1 - 0.3 * hop;
  }
  const opacity = smoothstep(t / 0.3);
  return { visible: opacity > 0, opacity, dy: -drop * lift, shadow };
};

// --------------------------------------------------------------------------- переходы

/**
 * Переход на склейке, начале мысли или ударном слове (`src/core/transitions.py`):
 * видео на несколько кадров сдвинуто, увеличено или засвечено и возвращается
 * ровно на место. Двигается ТОЛЬКО видео — субтитры и графика стоят.
 *
 * Требования те же, что у элемента: не короче восьми кадров, после последнего
 * кадра — ровно покой, никакой случайности. И своё: кадр ни на одном кадре не
 * показывает край видео. Сдвиг всегда идёт с запасом масштаба, который его
 * покрывает: масштаб вокруг центра на `s` даёт с каждой стороны `(s - 1) / 2`
 * ширины, поэтому `s - 1` не меньше удвоенного сдвига в долях кадра.
 */
/** Наезд: на сколько кадр крупнее в первый кадр перехода. */
export const CUT_PUNCH = 0.12;
/** Въезд сбоку: сдвиг в долях ширины кадра. */
export const CUT_SLIDE_RATIO = 0.07;
/** Тряска: амплитуда в долях ширины кадра и число колебаний за переход. */
export const CUT_SHAKE_RATIO = 0.012;
export const CUT_SHAKE_TURNS = 3;
/** Вспышка: непрозрачность белого в первый кадр. */
export const CUT_FLASH = 0.75;
/** Запас масштаба сверх сдвига — от субпиксельной щели на краю. */
export const CUT_COVER = 0.01;

const CUT_REST = Object.freeze({ scale: 1, dx: 0, dy: 0, flash: 0 });

/**
 * Состояние перехода на кадре: масштаб и сдвиг видео (вокруг центра кадра) и
 * непрозрачность белой вспышки. Вне переходов — покой, тот же объект.
 */
export const cutAt = (cuts, frame, fps, width, height) => {
  if (!Array.isArray(cuts)) return CUT_REST;
  for (const cut of cuts) {
    if (!cut || typeof cut !== "object") continue;
    const f0 = frameOf(cut.at, fps);
    const frames = animFrames(cut.frames);
    if (!(frame >= f0 && frame < f0 + frames)) continue;
    const t = (frame - f0) / frames;
    // Сила перехода: единица на первом кадре, быстро спадает к нулю.
    const k = 1 - easeOut(t);
    if (cut.kind === "punch") {
      return { scale: 1 + CUT_PUNCH * k, dx: 0, dy: 0, flash: 0 };
    }
    if (cut.kind === "slide_left" || cut.kind === "slide_right") {
      const side = cut.kind === "slide_left" ? 1 : -1;
      return {
        scale: 1 + (2 * CUT_SLIDE_RATIO + CUT_COVER) * k,
        dx: side * CUT_SLIDE_RATIO * (Number(width) || 0) * k,
        dy: 0,
        flash: 0,
      };
    }
    if (cut.kind === "shake") {
      const reach = CUT_SHAKE_RATIO * (Number(width) || 0) * k;
      return {
        scale: 1 + (2 * CUT_SHAKE_RATIO + CUT_COVER) * k,
        dx: reach * Math.sin(TAU * CUT_SHAKE_TURNS * t),
        dy: 0.6 * reach * Math.sin(TAU * CUT_SHAKE_TURNS * 1.3 * t + 1.1),
        flash: 0,
      };
    }
    if (cut.kind === "flash") {
      return { scale: 1, dx: 0, dy: 0, flash: CUT_FLASH * k };
    }
  }
  return CUT_REST;
};