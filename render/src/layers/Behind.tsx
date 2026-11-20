/**
 * Заголовок за головой: маска человека на блоке титра.
 *
 * Порядок слоёв не меняется — субтитры по-прежнему лежат поверх видео. На блок,
 * который «уходит за голову», накладывается покадровая маска «НЕ человек»
 * (альфа = 1 там, где фона не закрывает человек), и текст исчезает ровно там,
 * где стоит голова. Второго видео в кадре нет: ни второго декодирования, ни шва
 * по цвету между двумя копиями человека.
 *
 * Маски считает Python (`src/pipeline/matte.py`) и кладёт по файлу на кадр:
 * `<dir>/NNNNN.png`, номер — кадр выходного ролика. Здесь решений нет (§0):
 * нужно только, чтобы маска кадра была ГОТОВА к моменту, когда кадр снимают.
 * Remotion ждёт `delayRender`, но про картинку в CSS `mask-image` он не знает,
 * поэтому все маски грузятся один раз до первого кадра, декодируются и дальше
 * ставятся как `data:`-адреса: браузеру нечего качать посреди рендера.
 */
import React from "react";
import { continueRender, delayRender, staticFile } from "remotion";
import type { PlanBlock } from "../types";
import type { CameraState } from "./Camera";

/** Готовые маски: папка → адреса по кадрам от `first`. `null` — маски не загрузились. */
export type BehindMasks = Record<string, string[] | null>;

/** Имя файла маски кадра `n` — то же, что пишет `matte.write_masks` (`{n:05d}.png`). */
export const maskFile = (n: number): string => `${String(n).padStart(5, "0")}.png`;

const asDataUrl = (blob: Blob): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });

/** Маска загружена И декодирована: к кадру браузеру остаётся только нарисовать. */
const loadMask = async (url: string): Promise<{ src: string; image: HTMLImageElement }> => {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  const src = await asDataUrl(await response.blob());
  const image = new Image();
  image.src = src;
  await image.decode();
  return { src, image };
};

export const useBehindMasks = (blocks: PlanBlock[]): BehindMasks => {
  const [handle] = React.useState(() => delayRender("загрузка масок заголовка"));
  const [masks, setMasks] = React.useState<BehindMasks | null>(null);
  // Декодированные картинки держим за ссылку до конца рендера: выгруженную
  // браузер декодировал бы заново — уже посреди кадра.
  const keep = React.useRef<HTMLImageElement[]>([]);

  React.useEffect(() => {
    let alive = true;
    const wanted = blocks.flatMap((b) => (b.behind ? [b.behind] : []));
    Promise.all(
      wanted.map(async (behind): Promise<[string, string[] | null]> => {
        try {
          const loaded = await Promise.all(
            Array.from({ length: behind.last - behind.first + 1 }, (_, i) =>
              loadMask(staticFile(`${behind.dir}/${maskFile(behind.first + i)}`)),
            ),
          );
          keep.current.push(...loaded.map((m) => m.image));
          return [behind.dir, loaded.map((m) => m.src)];
        } catch (err) {
          // Всё или ничего: титр с маской на половине кадров мигал бы. Без
          // масок он просто рисуется поверх, как обычный.
          console.warn("маски заголовка не загрузились:", err);
          return [behind.dir, null];
        }
      }),
    ).then((entries) => {
      if (alive) setMasks(Object.fromEntries(entries));
    });
    return () => {
      alive = false;
    };
  }, [blocks]);

  // Отпускаем рендер ПОСЛЕ того, как маски попали в состояние и React
  // перерисовал кадр, а не из `.then`: иначе первый кадр снимется без маски.
  React.useEffect(() => {
    if (masks !== null) continueRender(handle);
  }, [masks, handle]);

  return masks ?? {};
};

/** Стиль маски для обёртки блока на этом кадре; пусто — блок рисуется поверх. */
export const behindStyle = (
  block: PlanBlock,
  frame: number,
  masks: BehindMasks,
  camera?: CameraState,
): React.CSSProperties => {
  const behind = block.behind;
  if (!behind) return {};
  const urls = masks[behind.dir];
  if (!urls || urls.length === 0) return {};
  // Вне диапазона держим крайнюю маску: блок в эти кадры уже почти прозрачен.
  const n = Math.min(behind.last, Math.max(behind.first, frame));
  const image = `url(${urls[n - behind.first]})`;
  // Маска снята с кадра ДО камеры, а человек в кадре стоит после неё. Та же
  // трансформа, что у видео: точка (ox, oy) на месте, остальное от неё
  // разъезжается в `scale` раз — то есть маска крупнее кадра и сдвинута.
  const scale = camera?.scale ?? 1;
  const size = `${(scale * 100).toFixed(3)}% ${(scale * 100).toFixed(3)}%`;
  const position =
    camera && scale !== 1
      ? `${(camera.ox * (1 - scale)).toFixed(2)}px ${(camera.oy * (1 - scale)).toFixed(2)}px`
      : "0px 0px";
  return {
    maskImage: image,
    WebkitMaskImage: image,
    // Обёртка блока — весь кадр, поэтому маска кадра ложится на неё один к одному.
    maskSize: size,
    WebkitMaskSize: size,
    maskPosition: position,
    WebkitMaskPosition: position,
    maskRepeat: "no-repeat",
    WebkitMaskRepeat: "no-repeat",
  };
};
