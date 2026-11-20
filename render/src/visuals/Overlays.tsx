/**
 * Графика поверх кадра: плашки, стикеры, декор, фото и карта.
 *
 * Рисуется всё, что лежит в `plan.meta.visuals`: свободные элементы
 * (`overlays`) и содержимое сцен (заголовок и `items` каждой сцены). Для
 * рендерера разницы между ними нет — у каждого свои секунды и свой
 * прямоугольник, а «элемент сцены» значит только то, что Python поставил его
 * на доску рядом с окном говорящего.
 *
 * Компонент ничего не решает (§0): место, время, кегль и цвета приезжают
 * готовыми, прямоугольник элемента — это и есть его коробка. Здесь только вид
 * (тени, обводки, рисунок карты) и движение, а движение — только `transform` и
 * `opacity` по кривым из `motion.mjs`. Ни `filter`, ни `backdrop-filter`:
 * ролик рендерится на процессоре с программным GL, и размытие на каждом кадре
 * там стоит дороже всей остальной графики. Тени — `box-shadow` и
 * `text-shadow`; мягкая тень вырезанного стикера рисуется ОДИН раз в канву
 * при загрузке картинки и дальше едет обычным битмапом.
 *
 * Картинки грузятся под `delayRender`: кадр не снимается, пока картинка не
 * декодирована. Если файла нет, элемент не рисуется вовсе, а в журнал идёт
 * предупреждение — графика украшение, и ронять из-за неё рендер ролика нельзя
 * (так же решено про шрифт в `Root.tsx` и вспышку в `Broll.tsx`).
 */

import React from "react";
import { continueRender, delayRender, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { behindStyle, useBehindMasks, type BehindMasks } from "../layers/Behind";
import { cameraAt, cameraShots } from "../layers/Camera";
import type { PlanBlock, Preset, RenderPlan } from "../types";
import {
  activeScene,
  frameOf,
  markAt,
  overlayAt,
  pinAt,
  prng,
  sceneProgress,
} from "./motion.mjs";
import { visualsOf, type DecorGlyph, type PlanOverlay, type PlanScene } from "./types";

// --------------------------------------------------------------------------- ассеты

/**
 * Папка ассетов задачи внутри бандла. `render.mjs` монтирует её как
 * `media/<jobKey>` и дописывает этот префикс к видео, звуку и шрифтам, а про
 * `meta.visuals` не знает. Поэтому префикс берётся из `plan.assetsPrefix`, если
 * вызывающий его передал, а иначе — из пути видео: оно лежит в той же папке.
 */
export const assetsPrefix = (plan: RenderPlan): string => {
  const own = (plan as RenderPlan & { assetsPrefix?: string | null }).assetsPrefix;
  if (typeof own === "string" && own) return own.replace(/\/+$/, "");
  const video = plan.video ?? "";
  const cut = video.lastIndexOf("/");
  return cut > 0 ? video.slice(0, cut) : "";
};

const assetPath = (prefix: string, asset: string): string =>
  prefix ? `${prefix}/${asset}` : asset;

export const assetUrl = (prefix: string, asset: string): string =>
  staticFile(assetPath(prefix, asset));

/**
 * Картинка, которую кадр ждёт: грузится и декодируется под `delayRender`, а в
 * дерево вставляется САМ декодированный элемент — браузеру к моменту снимка
 * остаётся только нарисовать. `onReady` получает картинку и узел-хозяин.
 *
 * Не `<Img>` из Remotion по одной причине: на отсутствующем файле он после
 * повторов отменяет весь рендер, а графика обязана выпадать, не роняя ролик.
 * `onFail` зовётся ДО того, как кадр отпущен: хозяин успевает убрать рамку
 * вокруг картинки, которой не будет.
 */
const useDecoded = (
  src: string,
  onReady: (image: HTMLImageElement, host: HTMLElement) => void,
  onFail?: () => void,
): React.RefObject<HTMLDivElement> => {
  const host = React.useRef<HTMLDivElement>(null);
  const ready = React.useRef(onReady);
  ready.current = onReady;
  const fail = React.useRef(onFail);
  fail.current = onFail;
  React.useLayoutEffect(() => {
    const node = host.current;
    if (!node) return;
    const handle = delayRender(`картинка графики: ${src.slice(-80)}`);
    let alive = true;
    const image = new Image();
    image.src = src;
    image
      .decode()
      .then(() => {
        if (alive) ready.current(image, node);
      })
      .catch((err) => {
        console.warn(`[visuals] картинка не загрузилась, элемент пропущен: ${src}`, err);
        if (alive) fail.current?.();
      })
      .finally(() => continueRender(handle));
    return () => {
      alive = false;
      node.replaceChildren();
      // Картинка уже не понадобится — держать из-за неё кадр незачем.
      continueRender(handle);
    };
  }, [src]);
  return host;
};

/** Обычная картинка во весь свой контейнер (`cover` или `contain`). */
export const Picture: React.FC<{
  src: string;
  fit: "cover" | "contain";
  position?: string;
  onFail?: () => void;
}> = ({ src, fit, position = "center", onFail }) => {
  const host = useDecoded(
    src,
    (image, node) => {
      image.style.cssText =
        `position:absolute;left:0;top:0;width:100%;height:100%;display:block;` +
        `object-fit:${fit};object-position:${position};`;
      node.replaceChildren(image);
    },
    onFail,
  );
  return <div ref={host} style={{ position: "absolute", inset: 0 }} />;
};

// --------------------------------------------------------------------------- цвет и шрифт

const EMOJI_FONT = '"ReelbotEmoji", "Noto Color Emoji", sans-serif';

const HEX = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

const channels = (hex: string): [number, number, number] => {
  let v = hex.slice(1);
  if (v.length === 3) {
    v = v
      .split("")
      .map((c) => c + c)
      .join("");
  }
  return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
};

/**
 * Цвет плана с прозрачностью. Цвета приезжают как `#RRGGBB`; всё остальное
 * (пустая строка, имя цвета) отдаётся как есть — подставлять свой цвет вместо
 * чужой ошибки рендерер не вправе.
 */
const tint = (color: string, opacity = 1): string => {
  if (!HEX.test(color)) return color || "transparent";
  const [r, g, b] = channels(color);
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, opacity))})`;
};

/** Светлый ли цвет: решает только, нужна ли тексту без подложки тёмная тень. */
const isLight = (color: string): boolean => {
  if (!HEX.test(color)) return true;
  const [r, g, b] = channels(color);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 150;
};

const textFont = (overlay: PlanOverlay, preset: Preset): React.CSSProperties => {
  const accent = overlay.font === "accent" ? (preset.accent_font ?? null) : null;
  if (accent) {
    return {
      fontFamily: `"ReelbotAccent", "${accent.family}", sans-serif`,
      fontWeight: accent.weight,
      // То же, что у выделенного слова в `Captions.tsx`: контекстных замен
      // замер Python не знает.
      fontFeatureSettings: '"calt" 0',
    };
  }
  return {
    fontFamily: `"ReelbotCaption", "${preset.font.family}", sans-serif`,
    fontWeight: preset.font.weight,
  };
};

// --------------------------------------------------------------------------- плашка

/**
 * Тень плашки. Размеры — от высоты самой плашки: тень в пикселях кадра у
 * мелкой подписи и у крупной капсулы выглядела бы по-разному тяжёлой.
 *
 * Внутренние тени — объём: светлая кромка сверху и затемнение снизу. Без них
 * капсула читается как плоская заливка из редактора.
 */
const chipShadow = (style: string | null, h: number): string => {
  const drop = [
    `0 ${(h * 0.14).toFixed(1)}px ${(h * 0.34).toFixed(1)}px rgba(0, 0, 0, 0.30)`,
    `0 ${(h * 0.03).toFixed(1)}px ${(h * 0.07).toFixed(1)}px rgba(0, 0, 0, 0.24)`,
  ];
  if (style === "tag") {
    return [...drop, `inset 0 -${(h * 0.05).toFixed(1)}px 0 rgba(0, 0, 0, 0.07)`].join(", ");
  }
  if (style === "label") return drop.join(", ");
  return [
    ...drop,
    `inset 0 ${(h * 0.035).toFixed(1)}px 0 rgba(255, 255, 255, 0.30)`,
    `inset 0 -${(h * 0.045).toFixed(1)}px 0 rgba(0, 0, 0, 0.12)`,
  ].join(", ");
};

/**
 * Плашка: коробка — ровно прямоугольник плана, текст с окошком значка стоят
 * в ней по центру одной группой.
 *
 * Строка собрана так же, как строка субтитра и плашка пункта в `Captions.tsx`:
 * слово — свой `inline-block`, между словами настоящий пробел. Этой моделью
 * Python мерил ширину текста, и другая разметка (один текстовый узел с
 * кернингом через пробел) дала бы другую ширину при том же шрифте.
 */
const Chip: React.FC<{ overlay: PlanOverlay; preset: Preset }> = ({ overlay, preset }) => {
  const words = overlay.words ?? [];
  const filled = Boolean(overlay.fill) && overlay.fill_opacity > 0;
  const color = overlay.color || "#FFFFFF";
  const icon = overlay.emoji && overlay.emoji_box > 0 ? overlay.emoji : null;
  const h = overlay.rect.height;
  const size = overlay.font_size;
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        // Рамка входит в прямоугольник плана, а не прибавляется к нему.
        boxSizing: "border-box",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: overlay.radius,
        backgroundColor: filled ? tint(overlay.fill, overlay.fill_opacity) : "transparent",
        border:
          overlay.border_width > 0 && overlay.border_color
            ? `${overlay.border_width}px solid ${overlay.border_color}`
            : undefined,
        boxShadow: filled ? chipShadow(overlay.style, h) : undefined,
        color,
        ...textFont(overlay, preset),
        fontSize: size,
        lineHeight: overlay.line_height,
        letterSpacing: `${overlay.letter_spacing}em`,
        wordSpacing: `${overlay.word_spacing}em`,
        whiteSpace: "nowrap",
        // Текст без подложки (заголовок сцены) держится на тени, но только
        // светлый: тень под тёмным текстом на светлом листе — это грязь.
        textShadow:
          !filled && isLight(color)
            ? `0 ${(size * 0.04).toFixed(1)}px ${(size * 0.16).toFixed(1)}px rgba(0, 0, 0, 0.45)`
            : "none",
      }}
    >
      {icon ? (
        <span
          style={{
            flex: "none",
            width: overlay.emoji_box,
            marginRight: overlay.emoji_gap,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: EMOJI_FONT,
            fontSize: overlay.emoji_size,
            fontWeight: 400,
            lineHeight: 1,
            letterSpacing: 0,
            wordSpacing: 0,
            // Цветной глиф: тень под ним читается как грязь.
            textShadow: "none",
          }}
        >
          {icon}
        </span>
      ) : null}
      <span style={{ display: "block", whiteSpace: "nowrap" }}>
        {words.map((word, i) => (
          <React.Fragment key={i}>
            {i > 0 ? <span>{" "}</span> : null}
            <span style={{ display: "inline-block" }}>{word}</span>
          </React.Fragment>
        ))}
      </span>
    </div>
  );
};

// --------------------------------------------------------------------------- стикер

/** Во сколько раз канва стикера плотнее кадра: запас на поворот и перелёт масштаба. */
const STICKER_DENSITY = 1.5;

/**
 * Вырезанный стикер: PNG с запечённой белой обводкой и мягкой тенью под ним.
 *
 * Тень обязана повторять ФОРМУ выреза, а `box-shadow` знает только
 * прямоугольник. Фильтр (`drop-shadow`) считал бы размытие на каждом кадре,
 * поэтому тень рисуется один раз — тенью канвы при загрузке картинки. Дальше
 * это готовый битмап, который двигают трансформом, как любую картинку.
 */
const StickerImage: React.FC<{ src: string; width: number; height: number }> = ({
  src,
  width,
  height,
}) => {
  const side = Math.max(width, height);
  // Поле под тень: размытие и сдвиг вниз не должны обрезаться краем канвы.
  const pad = Math.ceil(side * 0.2);
  const host = useDecoded(src, (image, node) => {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round((width + pad * 2) * STICKER_DENSITY);
    canvas.height = Math.round((height + pad * 2) * STICKER_DENSITY);
    canvas.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;display:block;";
    const ctx = canvas.getContext("2d");
    if (!ctx || !image.naturalWidth || !image.naturalHeight) return;
    // Картинка вписывается целиком: пропорцию стикера решает файл, не рамка.
    const fit = Math.min(width / image.naturalWidth, height / image.naturalHeight);
    const w = image.naturalWidth * fit;
    const h = image.naturalHeight * fit;
    ctx.scale(STICKER_DENSITY, STICKER_DENSITY);
    ctx.imageSmoothingQuality = "high";
    // Тень канвы живёт в пикселях канвы и масштаба контекста не слушается.
    ctx.shadowColor = "rgba(0, 0, 0, 0.36)";
    ctx.shadowBlur = side * 0.085 * STICKER_DENSITY;
    ctx.shadowOffsetY = side * 0.04 * STICKER_DENSITY;
    ctx.drawImage(image, pad + (width - w) / 2, pad + (height - h) / 2, w, h);
    node.replaceChildren(canvas);
  });
  return <div ref={host} style={{ position: "absolute", inset: -pad }} />;
};

/** Сплошная обводка глифа: кольцо из теней без размытия, плюс кольцо поменьше. */
const outlineShadow = (radius: number, color: string): string[] => {
  const out: string[] = [];
  for (const [r, count] of [
    [radius, 20],
    [radius * 0.5, 10],
  ]) {
    for (let i = 0; i < count; i++) {
      const a = (i / count) * Math.PI * 2;
      out.push(`${(Math.cos(a) * r).toFixed(2)}px ${(Math.sin(a) * r).toFixed(2)}px 0 ${color}`);
    }
  }
  return out;
};

/**
 * Стикер-глиф: эмодзи цветным шрифтом, когда картинки в наборе нет. Белая
 * обводка и тень — `text-shadow`: у цветного глифа тень рисуется по его
 * силуэту, так что получается тот же «вырезанный» вид, что у картинки.
 */
const StickerGlyph: React.FC<{ overlay: PlanOverlay }> = ({ overlay }) => {
  const size =
    overlay.glyph_size > 0
      ? overlay.glyph_size
      : Math.min(overlay.rect.width, overlay.rect.height) * 0.8;
  const edge = size * 0.055;
  const shadow = [
    ...outlineShadow(edge, "#FFFFFF"),
    // Тень лежит ПОД обводкой (в списке она последняя) и шире её, иначе из-под
    // белой каймы её не видно.
    `0 ${(edge + size * 0.035).toFixed(1)}px ${(size * 0.07).toFixed(1)}px rgba(0, 0, 0, 0.34)`,
    `0 ${(edge + size * 0.09).toFixed(1)}px ${(size * 0.2).toFixed(1)}px rgba(0, 0, 0, 0.30)`,
  ].join(", ");
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: EMOJI_FONT,
        fontSize: size,
        fontWeight: 400,
        lineHeight: 1,
        letterSpacing: 0,
        whiteSpace: "nowrap",
        textShadow: shadow,
      }}
    >
      {overlay.emoji}
    </div>
  );
};

// --------------------------------------------------------------------------- декор

type GlyphShape = { box: [number, number]; draw: (color: string) => React.ReactNode };

/**
 * Знаки декора — контуры, а не буквы шрифта: в шрифте темы «?» может оказаться
 * тонким или рукописным, а знак обязан читаться с любой темой одинаково.
 * Линии с круглыми концами дают тот же вид, что жирный скруглённый гротеск.
 */
const GLYPHS: Record<DecorGlyph, GlyphShape> = {
  question: {
    box: [100, 140],
    draw: (color) => (
      <>
        <path
          d="M27 46 A23 23 0 1 1 66.26 62.26 C58.3 70.3 50 78 50 90 L50 93"
          fill="none"
          stroke={color}
          strokeWidth={20}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx={50} cy={124} r={11.5} fill={color} />
      </>
    ),
  },
  exclaim: {
    box: [100, 140],
    draw: (color) => (
      <>
        <path d="M34 22 C34 10 66 10 66 22 L58 92 C57 102 43 102 42 92 Z" fill={color} />
        <circle cx={50} cy={124} r={11.5} fill={color} />
      </>
    ),
  },
  sparkle: {
    box: [100, 100],
    draw: (color) => (
      <path
        d="M50 2 C54 32 68 46 98 50 C68 54 54 68 50 98 C46 68 32 54 2 50 C32 46 46 32 50 2 Z"
        fill={color}
      />
    ),
  },
  arrow: {
    box: [100, 100],
    draw: (color) => (
      <g fill="none" stroke={color} strokeWidth={11} strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 78 C22 40 52 22 84 26" />
        <path d="M60 12 L86 26 L66 48" />
      </g>
    ),
  },
};

/**
 * Декор: россыпь знаков. Каждый знак живёт своей жизнью (`markAt`): проступает
 * со своей задержкой и покачивается со своей фазой. Центр и кегль знака —
 * числа плана; кегль здесь — высота знака.
 */
const Decor: React.FC<{ overlay: PlanOverlay }> = ({ overlay }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const shape = overlay.glyph ? GLYPHS[overlay.glyph] : undefined;
  const marks = overlay.marks ?? [];
  if (!shape || marks.length === 0) return null;
  if (frame <= frameOf(overlay.start, fps) || frame >= frameOf(overlay.end, fps)) return null;
  const color = overlay.color || "#FFFFFF";
  const [bw, bh] = shape.box;
  return (
    <>
      {marks.map((mark, i) => {
        const state = markAt(overlay, i, frame, fps);
        if (!state.visible) return null;
        const height = mark.size;
        const width = (mark.size * bw) / bh;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: mark.x - width / 2,
              top: mark.y - height / 2,
              width,
              height,
              opacity: state.opacity,
              transform:
                `translate(${state.dx.toFixed(2)}px, ${state.dy.toFixed(2)}px) ` +
                `rotate(${state.rotate.toFixed(3)}deg) scale(${state.scale.toFixed(4)})`,
              willChange: "transform, opacity",
            }}
          >
            <svg
              viewBox={`0 0 ${bw} ${bh}`}
              width="100%"
              height="100%"
              style={{ display: "block", overflow: "visible" }}
            >
              {/*
                Тёмный оттиск со сдвигом вниз: полупрозрачный белый знак на
                светлой стене без него пропадает. Копия контура, а не фильтр —
                и группой, чтобы стрелка не темнела там, где её линии сходятся.
              */}
              <g transform={`translate(0 ${(bh * 0.03).toFixed(1)})`} opacity={0.32}>
                {shape.draw("#000000")}
              </g>
              {shape.draw(color)}
            </svg>
          </div>
        );
      })}
    </>
  );
};

// --------------------------------------------------------------------------- карточки

/** Тень карточки (фото, карта) — от её высоты, как у плашки. */
const cardShadow = (h: number): string =>
  [
    `0 ${(h * 0.07).toFixed(1)}px ${(h * 0.17).toFixed(1)}px rgba(0, 0, 0, 0.36)`,
    `0 ${(h * 0.012).toFixed(1)}px ${(h * 0.035).toFixed(1)}px rgba(0, 0, 0, 0.26)`,
  ].join(", ");

/**
 * Карточка с рамкой: фото и карта отличаются только содержимым. Рамка — поле
 * вокруг содержимого, залитое цветом рамки: у настоящего `border` внутренний
 * угол не скругляется вместе с содержимым, и в углах проступала бы щель.
 *
 * Подпись (`words`) — полоса цвета рамки по низу карточки. Контракт про неё
 * молчит, но лист её допускает (`SceneItem.text` у фото и карты), и потерять
 * текст молча хуже, чем нарисовать его самым очевидным способом.
 */
const Card: React.FC<{
  overlay: PlanOverlay;
  preset: Preset;
  children: React.ReactNode;
}> = ({ overlay, preset, children }) => {
  const border = Math.max(0, overlay.border_width);
  const frameColor = overlay.border_color || "#FFFFFF";
  const words = overlay.words ?? [];
  const caption = words.length > 0 && overlay.font_size > 0;
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        boxSizing: "border-box",
        padding: border,
        borderRadius: overlay.radius,
        backgroundColor: frameColor,
        boxShadow: cardShadow(overlay.rect.height),
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          position: "relative",
          flex: "1 1 0",
          minHeight: 0,
          overflow: "hidden",
          borderRadius: Math.max(0, overlay.radius - border),
          backgroundColor: "#D9D6CF",
        }}
      >
        {children}
      </div>
      {caption ? (
        <div
          style={{
            flex: "none",
            padding: `${overlay.padding_y}px ${overlay.padding_x}px 0`,
            textAlign: "center",
            whiteSpace: "nowrap",
            overflow: "hidden",
            color: overlay.color || "#111111",
            ...textFont(overlay, preset),
            fontSize: overlay.font_size,
            lineHeight: overlay.line_height,
            letterSpacing: `${overlay.letter_spacing}em`,
            wordSpacing: `${overlay.word_spacing}em`,
          }}
        >
          {words.join(" ")}
        </div>
      ) : null}
    </div>
  );
};

/**
 * Фото в рамке. Картинка смотрит чуть выше центра, как вставка b-roll: объект
 * в кадре обычно лежит в верхней трети. Не загрузилась — карточки нет совсем:
 * пустая белая рамка на доске хуже, чем отсутствие фото.
 */
const PhotoCard: React.FC<{ overlay: PlanOverlay; preset: Preset; src: string }> = ({
  overlay,
  preset,
  src,
}) => {
  const box = React.useRef<HTMLDivElement>(null);
  return (
    <div ref={box} style={{ position: "absolute", inset: 0 }}>
      <Card overlay={overlay} preset={preset}>
        <Picture
          src={src}
          fit="cover"
          position="center 30%"
          // Прямо в узле, а не через состояние: состояние React применит уже
          // после того, как кадр отпущен, и рамка успела бы попасть в снимок.
          onFail={() => {
            if (box.current) box.current.style.display = "none";
          }}
        />
      </Card>
    </div>
  );
};

// --------------------------------------------------------------------------- карта

const MAP = {
  land: "#F1EEE7",
  block: "#E6E1D6",
  park: "#CBE5BC",
  water: "#A6D2F1",
  road: "#FFFFFF",
  casing: "#D8D2C6",
  major: "#FFE7A0",
  majorCasing: "#EBCB6E",
};

type MapArt = {
  angle: number;
  cx: number;
  cy: number;
  reach: number;
  unit: number;
  xs: number[];
  ys: number[];
  blocks: { x: number; y: number; w: number; h: number }[];
  park: { x: number; y: number; w: number; h: number } | null;
  river: string;
  avenue: { x1: number; y1: number; x2: number; y2: number } | null;
};

/**
 * Рисунок карты из зерна: повёрнутая сетка улиц, кварталы, парк, река и одна
 * диагональ. Настоящих тайлов нет и быть не может (сеть в рендере закрыта, и
 * карта здесь — знак «это место», а не навигация), поэтому город выдуманный,
 * но одно зерно всегда даёт один и тот же город.
 *
 * Сетка привязана к метке: две главные улицы пересекаются ровно под ней, и
 * поворачивается всё вокруг неё же — метка на перекрёстке читается как адрес,
 * метка посреди квартала — как случайная точка.
 */
const buildMap = (
  seed: number,
  w: number,
  h: number,
  pin: { x: number; y: number } | null,
): MapArt => {
  const rand = prng(seed);
  const cx = pin ? pin.x : w / 2;
  const cy = pin ? pin.y : h / 2;
  const unit = Math.max(w, h) / 22;
  const cell = unit * 4.4;
  // С поворотом сетка обязана закрыть карточку целиком из любой точки.
  const reach = Math.hypot(w, h) + cell;
  const angle = (rand() * 2 - 1) * 24;

  const lines = (origin: number): number[] => {
    const out = [origin];
    for (let v = origin; v < origin + reach; ) {
      v += cell * (0.7 + 0.6 * rand());
      out.push(v);
    }
    for (let v = origin; v > origin - reach; ) {
      v -= cell * (0.7 + 0.6 * rand());
      out.unshift(v);
    }
    return out;
  };
  const xs = lines(cx);
  const ys = lines(cy);
  const ix = xs.indexOf(cx);
  const iy = ys.indexOf(cy);

  const blocks: MapArt["blocks"] = [];
  for (let i = 0; i + 1 < xs.length; i++) {
    for (let j = 0; j + 1 < ys.length; j++) {
      if (rand() < 0.38) {
        blocks.push({ x: xs[i], y: ys[j], w: xs[i + 1] - xs[i], h: ys[j + 1] - ys[j] });
      }
    }
  }

  // Парк — квартал через один от метки по диагонали: рядом, но не под ней.
  const px = ix + (rand() < 0.5 ? -2 : 1);
  const py = iy + (rand() < 0.5 ? -2 : 1);
  const park =
    px >= 0 && py >= 0 && px + 1 < xs.length && py + 1 < ys.length
      ? { x: xs[px], y: ys[py], w: xs[px + 1] - xs[px], h: ys[py + 1] - ys[py] }
      : null;

  // Река — по ту сторону от метки, где больше места, волной через всю карту.
  const side = cy > h / 2 ? -1 : 1;
  const base = cy + side * cell * (1.25 + 0.5 * rand());
  const step = reach / 3;
  const n = (v: number) => v.toFixed(1);
  let prevX = cx - reach;
  let prevY = base + cell * 0.5 * (rand() * 2 - 1);
  let river = `M${n(prevX)} ${n(prevY)}`;
  for (let k = 1; k <= 6; k++) {
    const x = cx - reach + step * k;
    const y = base + cell * 0.5 * (rand() * 2 - 1);
    // Опорные точки на середине шага и на высоте концов: касательные в узлах
    // горизонтальны, и волна идёт без изломов.
    const mid = (prevX + x) / 2;
    river += ` C${n(mid)} ${n(prevY)} ${n(mid)} ${n(y)} ${n(x)} ${n(y)}`;
    prevX = x;
    prevY = y;
  }

  let avenue: MapArt["avenue"] = null;
  if (rand() < 0.7) {
    const slope = ((35 + 25 * rand()) * (rand() < 0.5 ? -1 : 1) * Math.PI) / 180;
    const ax = cx + cell * 1.6 * (rand() * 2 - 1);
    const ay = cy - side * cell * (0.6 + 0.8 * rand());
    avenue = {
      x1: ax - Math.cos(slope) * reach,
      y1: ay - Math.sin(slope) * reach,
      x2: ax + Math.cos(slope) * reach,
      y2: ay + Math.sin(slope) * reach,
    };
  }

  return { angle, cx, cy, reach, unit, xs, ys, blocks, park, river, avenue };
};

const MapArtwork: React.FC<{ art: MapArt; width: number; height: number }> = ({
  art,
  width,
  height,
}) => {
  const { cx, cy, reach, unit } = art;
  const minor = unit * 0.62;
  const major = unit * 1.05;
  const edge = Math.max(1, unit * 0.09);
  const gap = minor / 2 + edge;
  const lo = -reach;
  const hi = reach;
  /** Улицы одного слоя: все вертикали и горизонтали сетки одним контуром. */
  const grid = (skipMajor: boolean): string => {
    let d = "";
    for (const x of art.xs) {
      if (skipMajor && x === cx) continue;
      d += `M${x.toFixed(1)} ${(cy + lo).toFixed(1)}V${(cy + hi).toFixed(1)}`;
    }
    for (const y of art.ys) {
      if (skipMajor && y === cy) continue;
      d += `M${(cx + lo).toFixed(1)} ${y.toFixed(1)}H${(cx + hi).toFixed(1)}`;
    }
    return d;
  };
  const minorPath = grid(true);
  const majorPath =
    `M${cx.toFixed(1)} ${(cy + lo).toFixed(1)}V${(cy + hi).toFixed(1)}` +
    `M${(cx + lo).toFixed(1)} ${cy.toFixed(1)}H${(cx + hi).toFixed(1)}`;
  const avenue = art.avenue;
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height="100%"
      preserveAspectRatio="none"
      style={{ position: "absolute", inset: 0, display: "block" }}
    >
      <g transform={`rotate(${art.angle.toFixed(2)} ${cx.toFixed(1)} ${cy.toFixed(1)})`}>
        <rect x={cx + lo} y={cy + lo} width={reach * 2} height={reach * 2} fill={MAP.land} />
        {art.blocks.map((b, i) => (
          <rect
            key={i}
            x={b.x + gap}
            y={b.y + gap}
            width={Math.max(0, b.w - gap * 2)}
            height={Math.max(0, b.h - gap * 2)}
            rx={unit * 0.25}
            fill={MAP.block}
          />
        ))}
        {art.park ? (
          <rect
            x={art.park.x + gap}
            y={art.park.y + gap}
            width={Math.max(0, art.park.w - gap * 2)}
            height={Math.max(0, art.park.h - gap * 2)}
            rx={unit * 0.7}
            fill={MAP.park}
          />
        ) : null}
        <path
          d={art.river}
          fill="none"
          stroke={MAP.water}
          strokeWidth={unit * 2.1}
          strokeLinecap="round"
        />
        {/* Сначала кромки всех улиц, потом их полотно: перекрёстки сливаются. */}
        <path d={minorPath} fill="none" stroke={MAP.casing} strokeWidth={minor + edge * 2} />
        {avenue ? (
          <line {...avenue} stroke={MAP.casing} strokeWidth={minor * 1.25 + edge * 2} />
        ) : null}
        <path d={minorPath} fill="none" stroke={MAP.road} strokeWidth={minor} />
        {avenue ? <line {...avenue} stroke={MAP.road} strokeWidth={minor * 1.25} /> : null}
        <path d={majorPath} fill="none" stroke={MAP.majorCasing} strokeWidth={major + edge * 2} />
        <path d={majorPath} fill="none" stroke={MAP.major} strokeWidth={major} />
      </g>
    </svg>
  );
};

/** Метка карты: капля остриём в точку плана, под ней — пятно тени. */
const MapPin: React.FC<{ overlay: PlanOverlay; pin: { x: number; y: number } }> = ({
  overlay,
  pin,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const state = pinAt(overlay, frame, fps);
  if (!state.visible) return null;
  const tall = Math.max(44, Math.min(132, overlay.rect.height * 0.34));
  const wide = tall * 0.75;
  const color = overlay.color || "#FF3B30";
  return (
    <>
      <div
        style={{
          position: "absolute",
          left: pin.x - tall * 0.3,
          top: pin.y - tall * 0.09,
          width: tall * 0.6,
          height: tall * 0.18,
          borderRadius: "50%",
          background: "radial-gradient(closest-side, rgba(0, 0, 0, 0.42), rgba(0, 0, 0, 0))",
          opacity: state.opacity * state.shadow,
          transform: `scale(${(0.5 + 0.5 * state.shadow).toFixed(3)})`,
        }}
      />
      <svg
        viewBox="0 0 60 80"
        width={wide}
        height={tall}
        style={{
          position: "absolute",
          left: pin.x - wide / 2,
          top: pin.y - tall * (78 / 80),
          display: "block",
          overflow: "visible",
          opacity: state.opacity,
          transform: `translateY(${state.dy.toFixed(2)}px)`,
          willChange: "transform, opacity",
        }}
      >
        <path
          d="M30 78 C30 78 6 49 6 30 A24 24 0 1 1 54 30 C54 49 30 78 30 78 Z"
          fill={color}
          stroke="#FFFFFF"
          strokeWidth={4}
          strokeLinejoin="round"
        />
        <circle cx={30} cy={30} r={9} fill="#FFFFFF" />
      </svg>
    </>
  );
};

const MapCard: React.FC<{ overlay: PlanOverlay; preset: Preset }> = ({ overlay, preset }) => {
  const border = Math.max(0, overlay.border_width);
  const width = Math.max(1, overlay.rect.width - border * 2);
  const height = Math.max(1, overlay.rect.height - border * 2);
  const pin = overlay.pin;
  // Метка плана считается от угла карточки, рисунок — от угла содержимого.
  const inner = pin ? { x: pin.x - border, y: pin.y - border } : null;
  const art = React.useMemo(
    () => buildMap(overlay.seed, width, height, inner),
    [overlay.seed, width, height, inner?.x, inner?.y],
  );
  return (
    <>
      <Card overlay={overlay} preset={preset}>
        <MapArtwork art={art} width={width} height={height} />
      </Card>
      {/* Метка — поверх карточки, а не внутри неё: падая, она выходит за рамку. */}
      {pin ? <MapPin overlay={overlay} pin={pin} /> : null}
    </>
  );
};

// --------------------------------------------------------------------------- элемент

type Context = {
  preset: Preset;
  prefix: string;
  masks: BehindMasks;
  plan: RenderPlan;
  scenes: PlanScene[];
};

/** Маски «не человек» элемента, уходящего за говорящего; путь — уже в бандле. */
const behindOf = (overlay: PlanOverlay, prefix: string): PlanBlock | null => {
  if (overlay.layer !== "behind" || !overlay.behind) return null;
  // `Behind.tsx` написан под блок субтитра, но читает у него одно поле.
  return {
    behind: { ...overlay.behind, dir: assetPath(prefix, overlay.behind.dir) },
  } as unknown as PlanBlock;
};

/**
 * Обёртка «за говорящим»: маска на весь кадр, под которой элемент двигается.
 *
 * Маска снята с кадра до сцены: пока окно говорящего уехало в карточку,
 * человек стоит в другом месте, и маска вырезала бы дыру мимо него. Поэтому
 * во время сцены элемент рисуется поверх — так же, как при незагруженных
 * масках.
 */
const Behind: React.FC<{
  overlay: PlanOverlay;
  block: PlanBlock;
  ctx: Context;
  children: React.ReactNode;
}> = ({ overlay, block, ctx, children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const shots = React.useMemo(() => cameraShots(ctx.plan), [ctx.plan.meta]);
  // Вне жизни элемента обёртки нет вовсе: пустой слой с маской на весь кадр
  // браузер всё равно считал бы на каждом кадре ролика.
  if (frame <= frameOf(overlay.start, fps) || frame >= frameOf(overlay.end, fps)) return null;
  const scene = activeScene(ctx.scenes, frame, fps);
  const staged = scene >= 0 && sceneProgress(ctx.scenes[scene], frame, fps) > 0;
  const mask = staged ? {} : behindStyle(block, frame, ctx.masks, cameraAt(shots, frame, fps));
  return <div style={{ position: "absolute", inset: 0, ...mask }}>{children}</div>;
};

const Element: React.FC<{ overlay: PlanOverlay; ctx: Context }> = ({ overlay, ctx }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const state = overlayAt(overlay, frame, fps);
  if (!state.visible) return null;
  const { rect } = overlay;

  let body: React.ReactNode = null;
  if (overlay.kind === "chip") {
    body = <Chip overlay={overlay} preset={ctx.preset} />;
  } else if (overlay.kind === "sticker") {
    body = overlay.asset ? (
      <StickerImage
        src={assetUrl(ctx.prefix, overlay.asset)}
        width={rect.width}
        height={rect.height}
      />
    ) : overlay.emoji ? (
      <StickerGlyph overlay={overlay} />
    ) : null;
  } else if (overlay.kind === "photo") {
    body = overlay.asset ? (
      <PhotoCard
        overlay={overlay}
        preset={ctx.preset}
        src={assetUrl(ctx.prefix, overlay.asset)}
      />
    ) : null;
  } else if (overlay.kind === "map") {
    body = <MapCard overlay={overlay} preset={ctx.preset} />;
  }
  if (body === null) return null;

  return (
    <div
      style={{
        position: "absolute",
        left: rect.x,
        top: rect.y,
        width: rect.width,
        height: rect.height,
        opacity: state.opacity,
        transform:
          `translate(${state.dx.toFixed(2)}px, ${state.dy.toFixed(2)}px) ` +
          `rotate(${state.rotate.toFixed(3)}deg) scale(${state.scale.toFixed(4)})`,
        willChange: "transform, opacity",
      }}
    >
      {body}
    </div>
  );
};

/**
 * Слой графики. Стоит между вставками и субтитрами: картинка b-roll закрывает
 * графику (она закрывает и говорящего), а текст субтитра всегда сверху.
 */
export const Overlays: React.FC<{ plan: RenderPlan }> = ({ plan }) => {
  const visuals = React.useMemo(() => visualsOf(plan), [plan.meta]);
  const prefix = assetsPrefix(plan);
  // Порядок рисования — порядок плана: содержимое сцен, потом свободные
  // элементы. Внутри сцены позже названный элемент лежит выше.
  const all = React.useMemo<PlanOverlay[]>(
    () =>
      visuals
        ? [
            ...visuals.scenes.flatMap((scene) => [
              ...(scene.heading ? [scene.heading] : []),
              ...(scene.items ?? []),
            ]),
            ...visuals.overlays,
          ]
        : [],
    [visuals],
  );
  const behind = React.useMemo(
    () => all.flatMap((overlay) => behindOf(overlay, prefix) ?? []),
    [all, prefix],
  );
  // Хук стоит до раннего выхода: он держит рендер, пока маски не загружены.
  const masks = useBehindMasks(behind);
  if (!visuals || all.length === 0) return null;
  const ctx: Context = { preset: plan.preset, prefix, masks, plan, scenes: visuals.scenes };
  return (
    <>
      {all.map((overlay, i) => {
        const node =
          overlay.kind === "decor" ? (
            <Decor key={i} overlay={overlay} />
          ) : (
            <Element key={i} overlay={overlay} ctx={ctx} />
          );
        const block = behindOf(overlay, prefix);
        return block ? (
          <Behind key={i} overlay={overlay} block={block} ctx={ctx}>
            {node}
          </Behind>
        ) : (
          node
        );
      })}
    </>
  );
};
