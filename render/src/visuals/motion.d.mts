/**
 * Типы для `motion.mjs`. Сам модуль — обычный JavaScript: его импортирует и
 * бандл Remotion, и пробник под голым Node, которому TypeScript не прочитать.
 */

import type { PlanOverlay, PlanScene } from "./types";

export const MIN_ANIM_FRAMES: number;
export const POP_FROM: number;
export const POP_OVERSHOOT: number;
export const POP_PEAK_AT: number;
export const POP_FADE: number;
export const POP_OUT: number;
export const SPIN_DEG: Record<string, number>;
export const SLIDE_RATIO: number;
export const SLIDE_MIN_PX: number;
export const SLIDE_MAX_PX: number;
export const FLOAT_PERIOD_S: number;
export const FLOAT_SWAY_DEG: number;
export const MARK_STAGGER_S: number;
export const MARK_FROM: number;
export const MARK_OUT: number;
export const MARK_PERIOD_S: number;
export const MARK_FLOAT_RATIO: number;
export const MARK_DRIFT_RATIO: number;
export const MARK_SWAY_DEG: number;
export const PIN_DROP_S: number;
export const PIN_DROP_RATIO: number;
export const PIN_FALL: number;
export const PIN_HOP: number;

/** `t*t*(3 - 2*t)`, `t` зажат в 0..1 — кривая сцены из контракта. */
export function smoothstep(t: number): number;

/** Секунды плана → номер кадра. */
export function frameOf(seconds: number, fps: number): number;

/** Длительность анимации элемента: число плана, но не меньше `MIN_ANIM_FRAMES`. */
export function animFrames(frames: number): number;

/** Генератор с зерном: одна и та же последовательность чисел 0..1 на каждое зерно. */
export function prng(seed: number): () => number;

/** Окно говорящего и видео внутри него на кадре. Все значения уже текущие. */
export type StageState = {
  /** Ход сцены, 0..1. Ноль — сцены нет, окно во весь кадр. */
  p: number;
  x: number;
  y: number;
  width: number;
  height: number;
  radius: number;
  /** Наклон окна, градусы. */
  tilt: number;
  /** Блок W×H внутри окна: `translate(tx px, ty px) scale(scale)` от угла 0 0. */
  scale: number;
  tx: number;
  ty: number;
};

export function sceneProgress(scene: PlanScene | null, frame: number, fps: number): number;

/** Номер сцены, идущей на кадре, или -1. */
export function activeScene(scenes: PlanScene[], frame: number, fps: number): number;

export function stageAt(
  scene: PlanScene | null,
  frame: number,
  fps: number,
  W: number,
  H: number,
): StageState;

/** Элемент на кадре. `rotate` — полный угол, наклон покоя уже внутри. */
export type OverlayState = {
  visible: boolean;
  opacity: number;
  scale: number;
  dx: number;
  dy: number;
  rotate: number;
};

export function overlayAt(overlay: PlanOverlay, frame: number, fps: number): OverlayState;

/** Задержка входа знака декора номер `index`, кадры от начала декора. */
export function markDelay(overlay: PlanOverlay, index: number, fps: number): number;

/** Знак декора на кадре: `opacity` уже с собственной прозрачностью знака. */
export function markAt(
  overlay: PlanOverlay,
  index: number,
  frame: number,
  fps: number,
): OverlayState;

/** Метка карты: `dy` — сдвиг острия от места, `shadow` — тень под ним, 0..1. */
export type PinState = {
  visible: boolean;
  opacity: number;
  dy: number;
  shadow: number;
};

export function pinAt(overlay: PlanOverlay, frame: number, fps: number): PinState;
