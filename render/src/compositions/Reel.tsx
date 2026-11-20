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
import { Overlays } from "../visuals/Overlays";
import { Stage } from "../visuals/Stage";

export const Reel: React.FC<RenderPlan> = (plan) => {
  const { preset } = plan;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const shots = React.useMemo(() => cameraShots(plan), [plan.meta]);
  const camera = cameraAt(shots, frame, fps);

  return (
    <AbsoluteFill style={{ backgroundColor: preset.background }}>
      <Stage plan={plan}>
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
      </Stage>

      {plan.audio ? <Audio src={staticFile(plan.audio)} /> : null}

      <Broll items={plan.broll ?? []} preset={preset} />
      <Overlays plan={plan} />
      <Captions blocks={plan.blocks ?? []} preset={preset} camera={camera} />
    </AbsoluteFill>
  );
};
