/**
 * Зеркало плановой части `src/core/overlay_models.py`: то, что лежит в
 * `plan.meta.visuals`. Рендерер ничего не считает и не решает — секунды,
 * прямоугольники, кегли и цвета приходят готовыми. Формулы движения сцены
 * записаны в шапке `overlay_models.py`, их код — `motion.mjs`.
 */

import type { Rect, RenderPlan } from "../types";

export type OverlayKind = "chip" | "sticker" | "decor" | "photo" | "map";
export type OverlayAnim =
  | "pop"
  | "slide"
  | "fade"
  | "from_left"
  | "from_right"
  | "drop"
  | "stamp"
  | "wipe";
export type OverlayLayer = "front" | "behind";
export type ChipStyle = "pill" | "tag" | "label" | "marker" | "heading";
export type DecorGlyph = "question" | "exclaim" | "sparkle" | "arrow" | "confetti";
export type SceneLayout = "card_top" | "paper" | "phone";
export type SceneBgKind = "paper" | "board" | "gradient" | "still";
export type SceneChrome = "none" | "phone" | "polaroid";

/** Один знак декора: центр в пикселях кадра. `phase` — сдвиг покачивания, 0..1. */
export type PlanMark = {
  x: number;
  y: number;
  size: number;
  rotate: number;
  opacity: number;
  phase: number;
};

export type PlanOverlay = {
  kind: OverlayKind;
  /** Секунды на выходном таймлайне, на сетке кадров. */
  start: number;
  end: number;
  /** Вход идёт `in_frames` кадров от `start`; выход ЗАКАНЧИВАЕТСЯ в `end`. */
  in_frames: number;
  out_frames: number;
  /** Место в покое, пиксели кадра. У декора — область россыпи. */
  rect: Rect;
  rotate: number;
  anim: OverlayAnim;
  layer: OverlayLayer;
  seed: number;

  /** Плашка: коробка та же, что у плашки пункта в `Captions.tsx`. */
  style: ChipStyle | null;
  words: string[] | null;
  font: "caption" | "accent";
  font_size: number;
  letter_spacing: number;
  word_spacing: number;
  line_height: number;
  padding_x: number;
  padding_y: number;
  radius: number;
  border_width: number;
  border_color: string;
  fill: string;
  fill_opacity: number;
  color: string;
  /** Значок плашки ИЛИ глиф стикера. */
  emoji: string | null;
  emoji_box: number;
  emoji_size: number;
  emoji_gap: number;
  /**
   * Счётчик: слова `words[first..last]` — одно число, оно набегает до `value`
   * за `frames` кадров от `start` (`motion.mjs`, `countAt`). Нет — текст стоит.
   */
  count: { first: number; last: number; value: number; frames: number } | null;

  /** Стикер и фото: путь относительно папки ассетов задачи. */
  asset: string | null;
  glyph_size: number;
  /** Амплитуда покачивания в покое, px; 0 — стоит. */
  float_px: number;

  glyph: DecorGlyph | null;
  marks: PlanMark[] | null;

  /** Карта: метка внутри `rect`, пиксели от его левого верхнего угла. */
  pin: { x: number; y: number } | null;

  /** Маски «не человек» для `layer: "behind"`. */
  behind: { dir: string; first: number; last: number } | null;
};

export type PlanSceneBg = {
  kind: SceneBgKind;
  color: string;
  color2: string;
  /** Шаг клетки листа, px. */
  grid: number;
  line_color: string;
  /** Заранее размытый кадр для `still`. */
  asset: string | null;
};

export type PlanScene = {
  layout: SceneLayout;
  start: number;
  end: number;
  in_frames: number;
  out_frames: number;
  /** Окно говорящего в покое. */
  window: Rect;
  radius: number;
  tilt: number;
  /** Видео внутри окна в покое: сдвиг и масштаб блока W×H. */
  scale: number;
  tx: number;
  ty: number;
  bg: PlanSceneBg;
  chrome: SceneChrome;
  bezel: number;
  bezel_color: string;
  heading: PlanOverlay | null;
  items: PlanOverlay[];
};

export type PlanVisuals = {
  version: number;
  overlays: PlanOverlay[];
  scenes: PlanScene[];
};

/** План без графики (старый или с выключенным флагом) — `null`, и слой молчит. */
export const visualsOf = (plan: RenderPlan): PlanVisuals | null => {
  const raw = (plan.meta ?? {}).visuals as PlanVisuals | undefined;
  if (!raw || typeof raw !== "object") return null;
  const overlays = Array.isArray(raw.overlays) ? raw.overlays : [];
  const scenes = Array.isArray(raw.scenes) ? raw.scenes : [];
  if (!overlays.length && !scenes.length) return null;
  return { version: raw.version ?? 1, overlays, scenes };
};
