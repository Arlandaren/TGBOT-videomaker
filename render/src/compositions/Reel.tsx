/**
 * Композиция ролика: видео-подложка + b-roll + субтитры.
 *
 * Камера — масштаб видео-слоя по готовым планам (`layers/Camera.ts`): медленный
 * наезд внутри плана и укрупнение на резе там, где кроп его дать не смог.
 * Скачок масштаба бывает только на резе, внутри плана он линеен: пружин и
 * синусов по номеру кадра, от которых прежняя камера дёргалась, здесь нет.
 * Субтитры и вставки стоят снаружи и с камерой не двигаются.
 *
 * Порядок слоёв снизу вверх: сцена с видео (`visuals/Stage`) → b-roll → графика
 * (`visuals/Overlays`) → субтитры. Субтитры всегда сверху: картинка не должна
 * перекрывать текст. Без графики в плане (`meta.visuals` нет) `Stage` отдаёт
 * видео как есть, а `Overlays` ничего не рисует — ролик прежний.
 *
 * Камера остаётся ВНУТРИ сцены: наезд масштабирует видео в его собственных
 * координатах, а сцена потом уменьшает и сдвигает уже готовый кадр.
 */

import React from "react";
import { Video } from "@remotion/media";
import { AbsoluteFill, Audio, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import type { RenderPlan } from "../types";
import { Broll } from "../layers/Broll";
import { cameraAt, cameraShots, cameraStyle } from "../layers/Camera";
import { Captions } from "../layers/Captions";
import { CutFlash, ProgressBar, cutStyle, useCut } from "../layers/Cuts";
import { Overlays } from "../visuals/Overlays";
import { Stage } from "../visuals/Stage";

/**
 * Слои для быстрого рендера (`src/pipeline/fastrender.py`). Там видео, камеру,
 * переходы, полосу и простые субтитры рисует ffmpeg, а браузер снимает только
 * те кадры и слои, которые ffmpeg не умеет:
 *
 * - `overlay` — графика и «браузерные» блоки субтитров на прозрачном фоне,
 *   без видео: ffmpeg кладёт их поверх своего кадра;
 * - `under` — весь кадр, кроме простых субтитров и полосы: там, где слою нужны
 *   пиксели видео (b-roll с размытой подложкой, окно сцены, стеклянная панель).
 *
 * `browserBlocks` — номера блоков, которые рисует браузер (заголовок, плашка,
 * значок, панель, уход за голову); остальные блоки рисует libass.
 */
type FastLayers = { layers?: "overlay" | "under"; browserBlocks?: number[] };

export const Reel: React.FC<RenderPlan> = (plan) => {
  const { preset } = plan;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const shots = React.useMemo(() => cameraShots(plan), [plan.meta]);
  const camera = cameraAt(shots, frame, fps);
  // Переход (`layers/Cuts.tsx`) двигает кадр, уже увеличенный камерой.
  const cut = useCut(plan);
  const { layers, browserBlocks } = plan as RenderPlan & FastLayers;
  const ownBlocks = React.useMemo(() => {
    if (!layers) return plan.blocks ?? [];
    const keep = new Set(browserBlocks ?? []);
    return (plan.blocks ?? []).filter((_, i) => keep.has(i));
  }, [plan.blocks, layers, browserBlocks]);

  if (layers === "overlay") {
    return (
      <AbsoluteFill>
        <Overlays plan={plan} />
        <Captions blocks={ownBlocks} preset={preset} camera={camera} />
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill style={{ backgroundColor: preset.background }}>
      <Stage plan={plan}>
      <AbsoluteFill style={cutStyle(cut)}>
      <AbsoluteFill style={cameraStyle(camera)}>
        {/*
          `<Video>` из `@remotion/media`, а не `OffthreadVideo`: кадр декодирует
          сам Chromium через WebCodecs, без отдельного процесса-компоситора и
          прокси-сервера кадров. Замер на 31.8 с ролике, один поток: 225 → 132 с
          рендера, пик памяти 4.0 → 0.87 ГБ; кадры совпадают по времени, PSNR
          46 дБ. Файл здесь всегда собран нашим ffmpeg (`cut.py`/`crop.py`) и
          не фрагментирован: на фрагментированном MP4 (`mdhd duration 0`, сдвиг
          B-кадров только в `elst`) @remotion/media 4.0.520 показывал кадр на два
          раньше — замерено на клипе со стока, пока он был.
          Если WebCodecs кадр не берёт, компонент сам откатывается на
          `OffthreadVideo`.
        */}
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
      </AbsoluteFill>
      </Stage>
      <CutFlash cut={cut} />

      {plan.audio ? <Audio src={staticFile(plan.audio)} /> : null}

      <Broll items={plan.broll ?? []} preset={preset} />
      <Overlays plan={plan} />
      <Captions blocks={ownBlocks} preset={preset} camera={camera} />
      {layers ? null : <ProgressBar plan={plan} />}
    </AbsoluteFill>
  );
};
