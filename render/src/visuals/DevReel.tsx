/**
 * Композиция «ReelVisuals»: тот же ролик, что `compositions/Reel.tsx`, плюс
 * слой графики — сцена вокруг видео и элементы поверх него.
 *
 * Отдельная композиция, а не правка `Reel.tsx`, потому что тот файл сейчас
 * правит другая сессия: слои здесь повторяют его один в один, и когда графика
 * переедет в боевую композицию, от этого файла останутся две строки — `<Stage>`
 * вокруг слоя камеры и `<Overlays>` между вставками и субтитрами.
 *
 * Порядок слоёв снизу вверх: фон сцены и видео в окне (`Stage`) → b-roll →
 * графика (`Overlays`) → субтитры. Вставка закрывает графику так же, как
 * закрывает говорящего; субтитры всегда сверху — картинка не должна
 * перекрывать текст.
 *
 * Шрифты грузятся так же, как в `Root.tsx`: те же три лица под теми же именами
 * и с теми же объявлениями. Хуки оттуда не экспортируются, поэтому здесь их
 * копия; разойдись объявление — и ширина текста в плашке перестала бы
 * совпадать с той, что намерил Python (`tests/test_font_contract.py` сторожит
 * оригинал).
 */

import React from "react";
import { Video } from "@remotion/media";
import {
  AbsoluteFill,
  Audio,
  Composition,
  continueRender,
  delayRender,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { Broll } from "../layers/Broll";
import { cameraAt, cameraShots, cameraStyle } from "../layers/Camera";
import { Captions } from "../layers/Captions";
import { EMPTY_PLAN, type RenderPlan } from "../types";
import { Overlays } from "./Overlays";
import { Stage } from "./Stage";

export const DevReel: React.FC<RenderPlan> = (plan) => {
  const { preset } = plan;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const shots = React.useMemo(() => cameraShots(plan), [plan.meta]);
  const camera = cameraAt(shots, frame, fps);

  return (
    <AbsoluteFill style={{ backgroundColor: preset.background }}>
      {/*
        Камера остаётся ВНУТРИ сцены: наезд масштабирует видео в его
        собственных координатах, а сцена потом уменьшает и сдвигает уже
        готовый кадр. Наоборот наезд раздувал бы саму карточку.
      */}
      <Stage plan={plan}>
        <AbsoluteFill style={cameraStyle(camera)}>
          {plan.video ? (
            <Video
              src={staticFile(plan.video)}
              objectFit="cover"
              style={{ width: "100%", height: "100%" }}
              // Звук берём из этой же дорожки, если отдельной нет.
              muted={Boolean(plan.audio)}
            />
          ) : null}
        </AbsoluteFill>
      </Stage>

      {plan.audio ? <Audio src={staticFile(plan.audio)} /> : null}

      <Broll items={plan.broll ?? []} preset={preset} />
      <Overlays plan={plan} />
      <Captions blocks={plan.blocks ?? []} preset={preset} camera={camera} />
    </AbsoluteFill>
  );
};

/**
 * Шрифт из бандла под заданным именем. Объявления — как в `Root.tsx`: текст и
 * акцент с диапазоном весов `1 1000` (один файл закрывает весь диапазон, и
 * Chromium не синтезирует жирность), эмодзи — без дескрипторов.
 */
const useFont = (
  family: string,
  url: string | null | undefined,
  descriptors: FontFaceDescriptors | undefined,
  label: string,
) => {
  const [handle] = React.useState(() => delayRender(label));
  React.useEffect(() => {
    if (!url) {
      continueRender(handle);
      return;
    }
    const face = new FontFace(family, `url(${staticFile(url)})`, descriptors);
    face
      .load()
      .then((loaded) => {
        // В типах lib.dom для этой версии TS у FontFaceSet нет add().
        (document.fonts as unknown as { add(f: FontFace): void }).add(loaded);
        continueRender(handle);
      })
      .catch((err) => {
        // Лучше отрендерить системным шрифтом, чем не отрендерить вовсе.
        console.warn(`${label}: не удалось`, err);
        continueRender(handle);
      });
  }, [family, url, descriptors, label, handle]);
};

const RANGE: FontFaceDescriptors = { weight: "1 1000" };

const DevReelWithFont: React.FC<RenderPlan> = (plan) => {
  useFont("ReelbotCaption", plan.fontUrl, RANGE, "загрузка шрифта субтитров");
  useFont("ReelbotAccent", plan.accentFontUrl, RANGE, "загрузка второго шрифта субтитров");
  useFont("ReelbotEmoji", plan.emojiFontUrl, undefined, "загрузка шрифта эмодзи");
  return <DevReel {...plan} />;
};

/** Те же свойства, шрифты и метаданные, что у композиции «Reel» в `Root.tsx`. */
export const VisualsComposition: React.FC = () => (
  <Composition
    id="ReelVisuals"
    component={DevReelWithFont}
    defaultProps={EMPTY_PLAN}
    durationInFrames={Math.round(EMPTY_PLAN.duration * EMPTY_PLAN.fps)}
    fps={EMPTY_PLAN.fps}
    width={EMPTY_PLAN.width}
    height={EMPTY_PLAN.height}
    calculateMetadata={({ props }) => ({
      durationInFrames: Math.max(1, Math.round(props.duration * props.fps)),
      fps: props.fps,
      width: props.width,
      height: props.height,
    })}
  />
);
