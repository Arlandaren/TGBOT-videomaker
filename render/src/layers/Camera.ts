/**
 * Камера: масштаб видео-слоя по готовым планам (`src/core/camera.py`).
 *
 * Решений здесь нет (§0): планы приезжают в `plan.meta.camera` с готовыми
 * секундами, масштабами и точкой масштаба, а рендерер считает одну линейную
 * интерполяцию. Никаких пружин и синусов по номеру кадра — на них сгорела
 * прежняя камера (AUDIT §8.2). Скачок масштаба бывает только на границе планов,
 * то есть на резе.
 *
 * Масштабируется ТОЛЬКО видео. Субтитры и вставки стоят снаружи.
 */
import type React from "react";
import type { PlanShot, RenderPlan } from "../types";

export type CameraState = { scale: number; ox: number; oy: number };

const REST: CameraState = { scale: 1, ox: 0, oy: 0 };

const isShot = (v: unknown): v is PlanShot => {
  if (typeof v !== "object" || v === null) return false;
  const s = v as Record<string, unknown>;
  return ["start", "end", "scale_from", "scale_to", "ox", "oy"].every(
    (k) => typeof s[k] === "number" && Number.isFinite(s[k] as number),
  );
};

/** Планы камеры из плана рендера; план без них (старый, чужой) — без камеры. */
export const cameraShots = (plan: RenderPlan): PlanShot[] => {
  const raw = plan.meta?.camera;
  return Array.isArray(raw) ? raw.filter(isShot) : [];
};

/**
 * Состояние камеры на кадре. Зеркало `camera.state_at` в Python: та же
 * арифметика на номерах кадров. До первого плана и между планами камеры нет;
 * после последнего держится его конец — файл бывает на кадр длиннее листа.
 */
export const cameraAt = (shots: PlanShot[], frame: number, fps: number): CameraState => {
  let last: PlanShot | null = null;
  for (const shot of shots) {
    const first = Math.round(shot.start * fps);
    const stop = Math.round(shot.end * fps);
    if (frame < first) break;
    last = shot;
    if (frame < stop) {
      const p = (frame - first) / Math.max(1, stop - first);
      return {
        scale: shot.scale_from + (shot.scale_to - shot.scale_from) * p,
        ox: shot.ox,
        oy: shot.oy,
      };
    }
  }
  if (last !== null && last === shots[shots.length - 1]) {
    return { scale: last.scale_to, ox: last.ox, oy: last.oy };
  }
  return REST;
};

/** Стиль обёртки видео. Камера в покое — пусто: слой остаётся как был. */
export const cameraStyle = (cam: CameraState): React.CSSProperties =>
  cam.scale === 1
    ? {}
    : {
        transform: `scale(${cam.scale.toFixed(5)})`,
        transformOrigin: `${cam.ox}px ${cam.oy}px`,
      };
