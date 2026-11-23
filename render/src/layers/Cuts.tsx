/**
 * Переходы и полоса прогресса: движение видео на склейке и линия сверху.
 *
 * Решений здесь нет (§0): переходы приезжают в `plan.meta.cuts` с готовыми
 * секундами, кадрами и видом (`src/core/transitions.py`), формулу считает
 * `visuals/motion.mjs` (`cutAt`). Двигается только `transform` и `opacity`.
 *
 * `cutStyle` — обёртка ВОКРУГ слоя камеры: переход двигает уже увеличенный
 * камерой кадр вокруг центра. `CutFlash` — белая вспышка поверх видео, под
 * вставками, графикой и субтитрами.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import type { RenderPlan } from "../types";
import { cutAt, type CutState } from "../visuals/motion.mjs";

/** Состояние перехода на текущем кадре. */
export const useCut = (plan: RenderPlan): CutState => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  return cutAt(plan.meta?.cuts, frame, fps, width, height);
};

/** Стиль обёртки видео. Вне перехода — пусто: слой остаётся как был. */
export const cutStyle = (cut: CutState): React.CSSProperties =>
  cut.scale === 1 && cut.dx === 0 && cut.dy === 0
    ? {}
    : {
        transform:
          `translate(${cut.dx.toFixed(2)}px, ${cut.dy.toFixed(2)}px) ` +
          `scale(${cut.scale.toFixed(5)})`,
        transformOrigin: "50% 50%",
      };

/** Белая вспышка перехода `flash`. Вне его слоя нет вовсе. */
export const CutFlash: React.FC<{ cut: CutState }> = ({ cut }) =>
  cut.flash > 0 ? (
    <AbsoluteFill style={{ backgroundColor: "#FFFFFF", opacity: cut.flash }} />
  ) : null;

type Progress = { color: string; height: number };

const progressOf = (plan: RenderPlan): Progress | null => {
  const raw = plan.meta?.progress as Partial<Progress> | undefined;
  if (!raw || typeof raw.color !== "string" || typeof raw.height !== "number") return null;
  return raw.height > 0 ? { color: raw.color, height: raw.height } : null;
};

/**
 * Полоса прогресса: линия сверху во всю ширину, растёт слева направо через
 * `scaleX` — раскладка не пересчитывается ни на одном кадре.
 */
export const ProgressBar: React.FC<{ plan: RenderPlan }> = ({ plan }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const bar = progressOf(plan);
  if (!bar) return null;
  const done = Math.min(1, Math.max(0, (frame + 1) / Math.max(1, durationInFrames)));
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width: "100%",
        height: bar.height,
        backgroundColor: bar.color,
        transform: `scaleX(${done.toFixed(5)})`,
        transformOrigin: "0 50%",
      }}
    />
  );
};
