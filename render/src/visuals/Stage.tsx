/**
 * Сцена: окно говорящего уезжает из полного кадра в карточку, под ним — фон.
 *
 * `<Stage plan={plan}>{слой камеры}</Stage>` оборачивает видео и больше ничего:
 * вставки, графика и субтитры стоят снаружи и со сценой не двигаются.
 *
 * Главное правило — ДЕРЕВО ОДНО НА ВСЕ КАДРЫ. Сцены нет — узлы те же, только
 * стили нейтральные: окно во весь кадр, без скругления, наклона, тени и
 * обрезки. Стоило бы обёртке появляться только на время сцены, React
 * пересоздавал бы `<Video>` на её границах, а пересозданное видео — это
 * повторное открытие файла и пустой кадр посреди ролика. Нейтральное состояние
 * проверено на кадрах вне сцен: они совпадают с кадрами боевой композиции
 * `Reel` пиксель в пиксель.
 *
 * Геометрию компонент не считает: `stageAt` (`motion.mjs`) — формулы контракта
 * из шапки `src/core/overlay_models.py`, по ним же Python знает, где окно в
 * каждом кадре, и пересчитывает трек лица. Отсюда единственное место во всём
 * слое графики, где движется не только `transform`: на входе и выходе сцены
 * окно меняет размер и скругление — контракт задаёт его прямоугольником, а
 * обрезку с постоянным радиусом углов масштабом не выразить. Это дюжина кадров
 * на сцену; в покое сцены не меняется ничего.
 *
 * Слои снизу вверх: фон сцены → корпус рамки (телефон, полароид) → окно с
 * видео → детали рамки поверх экрана. Окно режет видео по скруглённому
 * прямоугольнику (`overflow: hidden`), а само видео внутри него — блок W×H с
 * трансформом контракта. Второй копии видео нет: фон «размытый кадр» — заранее
 * приготовленная картинка, а не фильтр поверх ролика.
 */

import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import type { RenderPlan } from "../types";
import { activeScene, stageAt } from "./motion.mjs";
import { Picture, assetUrl, assetsPrefix } from "./Overlays";
import { visualsOf, type PlanScene, type PlanSceneBg } from "./types";

const HEX = /^#[0-9a-f]{6}$/i;

/** Тот же цвет светлее: кромка корпуса телефона — не отдельное число плана. */
const lighten = (color: string, amount: number): string => {
  if (!HEX.test(color)) return color;
  const n = parseInt(color.slice(1), 16);
  const mix = (c: number) => Math.round(c + (255 - c) * amount);
  return `rgb(${mix(n >> 16)}, ${mix((n >> 8) & 255)}, ${mix(n & 255)})`;
};

// --------------------------------------------------------------------------- фон

/**
 * Лист в клетку — одними градиентами, без картинки: тонкая клетка, каждая
 * четвёртая линия толще (так разлинована настоящая миллиметровка), сверху
 * пятно света и лёгкое затемнение к краям. Ровная заливка с сеткой читается
 * как обои редактора, а не как бумага.
 */
const paperStyle = (bg: PlanSceneBg): React.CSSProperties => {
  const cell = bg.grid;
  const light =
    "radial-gradient(120% 80% at 22% 8%, rgba(255, 255, 255, 0.60), rgba(255, 255, 255, 0) 62%)";
  const shade =
    "radial-gradient(150% 115% at 50% 45%, rgba(70, 55, 30, 0) 60%, rgba(70, 55, 30, 0.16) 100%)";
  if (!(cell > 0)) {
    return { backgroundColor: bg.color, backgroundImage: `${light}, ${shade}` };
  }
  const thin = Math.max(1.5, cell / 30);
  const thick = thin * 1.9;
  const line = (deg: number, width: number) => {
    const edge = `${width.toFixed(2)}px`;
    return `linear-gradient(${deg}deg, ${bg.line_color} ${edge}, transparent ${edge})`;
  };
  const big = cell * 4;
  // Полклетки от края: линия по самой кромке кадра читается как рамка.
  const off = cell / 2;
  return {
    backgroundColor: bg.color,
    backgroundImage: [
      light,
      shade,
      line(180, thick),
      line(90, thick),
      line(180, thin),
      line(90, thin),
    ].join(", "),
    backgroundSize: [
      "100% 100%",
      "100% 100%",
      `${big}px ${big}px`,
      `${big}px ${big}px`,
      `${cell}px ${cell}px`,
      `${cell}px ${cell}px`,
    ].join(", "),
    backgroundPosition: ["0 0", "0 0", ...Array(4).fill(`${off}px ${off}px`)].join(", "),
  };
};

/** Свет сверху на тёмной доске: без него градиент выглядит плоской заливкой. */
const GLOW =
  "radial-gradient(110% 60% at 50% 0%, rgba(255, 255, 255, 0.09), rgba(255, 255, 255, 0) 70%)";

const backdropStyle = (bg: PlanSceneBg): React.CSSProperties => {
  if (bg.kind === "paper") return paperStyle(bg);
  if (bg.kind === "gradient") {
    return {
      backgroundColor: bg.color,
      backgroundImage: `${GLOW}, linear-gradient(180deg, ${bg.color}, ${bg.color2})`,
    };
  }
  // `board` и подложка под `still`: ровный цвет плана.
  return { backgroundColor: bg.color, backgroundImage: bg.kind === "board" ? GLOW : undefined };
};

/**
 * Фон одной сцены. Узел есть всегда, виден — только пока идёт его сцена:
 * картинка `still` грузится один раз в начале рендера, а не на первом кадре
 * сцены, а спрятанный узел браузер не рисует.
 */
const Backdrop: React.FC<{ bg: PlanSceneBg; shown: boolean; prefix: string }> = React.memo(
  ({ bg, shown, prefix }) => (
    <div
      style={{
        position: "absolute",
        inset: 0,
        overflow: "hidden",
        visibility: shown ? "visible" : "hidden",
        ...backdropStyle(bg),
      }}
    >
      {/*
        Кадр приезжает уже размытым И затемнённым (`overlays._still`): второе
        затемнение здесь утопило бы его в чёрном. Цвет плана остаётся под
        картинкой — им закрыт фон, если файл не загрузился.
      */}
      {bg.kind === "still" && bg.asset ? (
        <Picture src={assetUrl(prefix, bg.asset)} fit="cover" />
      ) : null}
    </div>
  ),
);

// --------------------------------------------------------------------------- рамка

/** Во сколько раз низ полароида толще остальных полей. */
const POLAROID_CHIN = 3.4;

/** Одна тень вниз: сдвиг, размытие (px) и плотность чёрного. */
const drop = (y: number, blur: number, alpha: number): string =>
  `0 ${y.toFixed(1)}px ${blur.toFixed(1)}px rgba(0, 0, 0, ${alpha.toFixed(3)})`;

/**
 * Тень окна без рамки: широкая мягкая, контактная и светлая кромка в волос.
 * Растёт вместе с ходом сцены — окно во весь кадр тени не отбрасывает.
 */
const windowShadow = (p: number, k: number): string | undefined =>
  p > 0
    ? [
        drop(34 * k * p, 84 * k * p, 0.34 * p),
        drop(8 * k * p, 20 * k * p, 0.24 * p),
        `0 0 0 ${(2 * k * p).toFixed(2)}px rgba(255, 255, 255, ${(0.14 * p).toFixed(3)})`,
      ].join(", ")
    : undefined;

export const Stage: React.FC<{ plan: RenderPlan; children: React.ReactNode }> = ({
  plan,
  children,
}) => {
  const frame = useCurrentFrame();
  const { fps, width: W, height: H } = useVideoConfig();
  const visuals = React.useMemo(() => visualsOf(plan), [plan.meta]);
  const scenes: PlanScene[] = visuals?.scenes ?? [];
  const prefix = assetsPrefix(plan);

  const index = activeScene(scenes, frame, fps);
  const scene = index >= 0 ? scenes[index] : null;
  const s = stageAt(scene, frame, fps, W, H);
  const p = s.p;
  // Размеры рамки и теней заданы в пикселях кадра 1920 и едут за его высотой.
  const k = H / 1920;

  const chrome = scene && p > 0 ? scene.chrome : "none";
  const bezel = scene && p > 0 ? p * scene.bezel : 0;
  const bezelColor = scene?.bezel_color || "#000000";
  const rim = chrome === "phone" ? 3 * k * p : 0;
  const rimColor = lighten(bezelColor, 0.16);
  const framed = chrome === "phone" || chrome === "polaroid";

  // Остров телефона — в долях ширины экрана, как у настоящего.
  const islandW = s.width * 0.27;
  const islandH = s.width * 0.078;
  const button = (side: "left" | "right", top: number, tall: number): React.CSSProperties => ({
    position: "absolute",
    [side]: -(bezel + rim + 5 * k),
    top: s.height * top,
    width: 7 * k,
    height: s.height * tall,
    borderRadius: 3 * k,
    backgroundColor: rimColor,
    opacity: chrome === "phone" ? p : 0,
  });

  return (
    <AbsoluteFill>
      {scenes.map((item, i) => (
        <Backdrop key={i} bg={item.bg} shown={i === index && p > 0} prefix={prefix} />
      ))}

      {/* Каркас: окно со всей рамкой наклоняется как одно целое, вокруг центра окна. */}
      <div
        style={{
          position: "absolute",
          left: s.x,
          top: s.y,
          width: s.width,
          height: s.height,
          transform: s.tilt !== 0 ? `rotate(${s.tilt.toFixed(3)}deg)` : undefined,
        }}
      >
        {/* Кнопки корпуса — под ним: из-за кромки торчат только их края. */}
        <div style={button("right", 0.25, 0.085)} />
        <div style={button("left", 0.15, 0.032)} />
        <div style={button("left", 0.21, 0.058)} />
        <div style={button("left", 0.285, 0.058)} />

        {/*
          Корпус рамки лежит ПОД окном и шире его на толщину рамки. Кольцо
          поверх окна оставляло бы волосяную щель между своей внутренней
          кромкой и скруглением видео — сглаживание двух краёв не совпадает.
        */}
        <div
          style={{
            position: "absolute",
            left: -bezel,
            right: -bezel,
            top: -bezel,
            bottom: chrome === "polaroid" ? -bezel * POLAROID_CHIN : -bezel,
            borderRadius: s.radius + bezel,
            backgroundColor: bezelColor,
            opacity: framed ? Math.min(1, p * 3) : 0,
            // Телефон — тёмный предмет на тёмном фоне, его тень плотнее;
            // у белого полароида такая же читалась бы как грязь вокруг рамки.
            boxShadow: framed
              ? [
                  chrome === "phone" ? `0 0 0 ${rim.toFixed(2)}px ${rimColor}` : null,
                  drop(46 * k * p, 110 * k * p, chrome === "phone" ? 0.55 : 0.34),
                  drop(10 * k * p, 26 * k * p, chrome === "phone" ? 0.38 : 0.22),
                ]
                  .filter(Boolean)
                  .join(", ")
              : undefined,
          }}
        />

        {/*
          Окно: режет видео по скруглённому прямоугольнику. Без сцены резать
          нечего, и обрезка снята: с ней Chromium растеризует слой камеры
          через промежуточную поверхность, и кадр без единой сцены отличался
          бы от боевого на единицу яркости в нескольких процентах пикселей.
        */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            overflow: p > 0 ? "hidden" : "visible",
            borderRadius: s.radius,
            boxShadow: framed ? undefined : windowShadow(p, k),
          }}
        >
          {/* Видео внутри окна: блок W×H от левого верхнего угла, трансформ контракта. */}
          <div
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              width: W,
              height: H,
              transformOrigin: "0 0",
              transform:
                p > 0
                  ? `translate(${s.tx.toFixed(3)}px, ${s.ty.toFixed(3)}px) ` +
                    `scale(${s.scale.toFixed(5)})`
                  : undefined,
            }}
          >
            {children}
          </div>
        </div>

        {/* Остров телефона с глазком камеры — поверх экрана. */}
        <div
          style={{
            position: "absolute",
            left: (s.width - islandW) / 2,
            top: s.width * 0.03,
            width: islandW,
            height: islandH,
            borderRadius: islandH / 2,
            backgroundColor: "#000000",
            boxShadow: "inset 0 0 0 1.5px rgba(255, 255, 255, 0.07)",
            opacity: chrome === "phone" ? p : 0,
          }}
        >
          <div
            style={{
              position: "absolute",
              right: islandH * 0.3,
              top: islandH * 0.31,
              width: islandH * 0.38,
              height: islandH * 0.38,
              borderRadius: "50%",
              background: "radial-gradient(circle at 35% 35%, #2A3350, #0A0C14 70%)",
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};
