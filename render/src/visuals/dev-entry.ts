/**
 * Вход бандла для предпросмотра графики (`render/visuals_preview.mjs`).
 *
 * Регистрирует обе композиции: боевую «Reel» из `Root.tsx` как есть и
 * «ReelVisuals» со слоем графики. Вторая рядом с первой, а не вместо неё:
 * один и тот же план можно снять обеими и сравнить кадр с графикой и без.
 *
 * Боевой вход `src/index.ts` этот файл не импортирует, поэтому в бандл
 * рендера ролика (`render.mjs`) отсюда не попадает ничего.
 */

import React from "react";
import { registerRoot } from "remotion";
import { RemotionRoot } from "../Root";
import { VisualsComposition } from "./DevReel";

const VisualsRoot: React.FC = () =>
  React.createElement(
    React.Fragment,
    null,
    React.createElement(RemotionRoot),
    React.createElement(VisualsComposition),
  );

registerRoot(VisualsRoot);
