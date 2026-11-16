import React from "react";
import { Composition, continueRender, delayRender, staticFile } from "remotion";
import { Reel } from "./compositions/Reel";
import { EMPTY_PLAN, type RenderPlan } from "./types";

/**
 * Шрифт грузится ИЗ БАНДЛА, а не из системы: Chromium в контейнере рендера
 * иначе подставит дефолт и кириллица поедет (§5 M5 ⚠).
 */
const useCaptionFont = (fontUrl: string | null | undefined) => {
  const [handle] = React.useState(() => delayRender("загрузка шрифта субтитров"));
  React.useEffect(() => {
    if (!fontUrl) {
      continueRender(handle);
      return;
    }
    // Диапазон весов, а не умолчание: без дескриптора лицо регистрируется как
    // `normal` (400), а Captions.tsx просит вес пресета — пара, на которой
    // Chromium вправе нарисовать синтетический жир. Замер показал, что на
    // нынешней сборке он ширину не меняет (объявленные 400 и 100 против
    // запрошенных 800 дали те же пиксели), то есть активной ошибки не было.
    // Объявление всё равно обязано совпадать с линейкой `render/measure.mjs`:
    // сверка 2 px верна ровно настолько, насколько рендер объявляет шрифт так
    // же, как страница замера. Сторожит `tests/test_font_contract.py`.
    const face = new FontFace("ReelbotCaption", `url(${staticFile(fontUrl)})`, {
      weight: "1 1000",
    });
    face
      .load()
      .then((loaded) => {
        // В типах lib.dom для этой версии TS у FontFaceSet нет add(), хотя в
        // Chromium он есть с 2016 года.
        (document.fonts as unknown as { add(f: FontFace): void }).add(loaded);
        continueRender(handle);
      })
      .catch((err) => {
        // Лучше отрендерить системным шрифтом, чем не отрендерить вовсе.
        console.warn("шрифт не загрузился:", err);
        continueRender(handle);
      });
  }, [fontUrl, handle]);
};

/**
 * Второй шрифт темы — им набрано выделенное слово (`preset.accent_font`).
 *
 * Отдельное лицо под своим именем, а не второй файл в «ReelbotCaption»: слово
 * выбирает шрифт по имени семейства (Captions.tsx, `Word`), и у двух файлов
 * под одним именем Chromium выбирал бы сам. Объявление — то же, что у первого
 * шрифта и у линейки `render/measure.mjs`.
 */
const useAccentFont = (accentFontUrl: string | null | undefined) => {
  const [handle] = React.useState(() => delayRender("загрузка второго шрифта субтитров"));
  React.useEffect(() => {
    if (!accentFontUrl) {
      continueRender(handle);
      return;
    }
    const face = new FontFace("ReelbotAccent", `url(${staticFile(accentFontUrl)})`, {
      weight: "1 1000",
    });
    face
      .load()
      .then((loaded) => {
        (document.fonts as unknown as { add(f: FontFace): void }).add(loaded);
        continueRender(handle);
      })
      .catch((err) => {
        console.warn("второй шрифт не загрузился:", err);
        continueRender(handle);
      });
  }, [accentFontUrl, handle]);
};

const ReelWithFont: React.FC<RenderPlan> = (plan) => {
  useCaptionFont(plan.fontUrl);
  useAccentFont(plan.accentFontUrl);
  return <Reel {...plan} />;
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Reel"
      component={ReelWithFont}
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
};
