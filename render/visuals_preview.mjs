#!/usr/bin/env node
/**
 * Предпросмотр графики: план → ролик или отдельные кадры композиции
 * «ReelVisuals» (`src/visuals/DevReel.tsx`).
 *
 *   node render/visuals_preview.mjs --plan <plan.json> --assets <dir> --out <mp4>
 *        [--from <кадр> --to <кадр>]
 *        [--stills 20,40,130 --stills-dir <dir>]
 *        [--job-key <ключ>] [--concurrency <n>] [--gl <режим>] [--composition <id>]
 *        [--crf <n>] [--timeout-ms <мс>]
 *
 * Кадры (`--stills`) снимаются `renderStill` и готовы за секунды — это быстрый
 * способ посмотреть, как выглядит элемент на входе, в покое и на выходе, не
 * дожидаясь ролика. `--out` и `--stills` можно дать вместе.
 *
 * `--composition Reel` снимает тот же план боевой композицией, без графики:
 * так сравнивают кадр «до и после» и время рендера слоя.
 *
 * Модель — `render.mjs`, отличий три. Бандл свой (`.cache/bundle-visuals`) со
 * своим входом (`src/visuals/dev-entry.ts`): боевой бандл предпросмотр не
 * трогает и не пересобирает. Запрос приходит флагами, а не JSON на stdin: это
 * инструмент для рук, а не стык с Python. И план читается как есть — пути в нём
 * уже обязаны быть именами файлов в `--assets` (`samples/plan-visuals.json`).
 *
 * Ключ задачи (`--job-key`) — имя точки монтирования ассетов внутри бандла.
 * Параллельные прогоны делят одну папку `public/media`, поэтому у каждого
 * прогона ключ должен быть свой; без флага он собирается из номера процесса.
 */

import { bundle } from "@remotion/bundler";
import {
  ensureBrowser,
  openBrowser,
  renderMedia,
  renderStill,
  selectComposition,
} from "@remotion/renderer";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENTRY = path.join(HERE, "src", "visuals", "dev-entry.ts");
const BUNDLE_DIR = path.join(HERE, ".cache", "bundle-visuals");
// staticFile() резолвится в /public/<path>, поэтому монтируем именно в public.
const MEDIA_DIR = path.join(BUNDLE_DIR, "public", "media");
const STAMP = path.join(HERE, ".cache", "bundle-visuals.stamp");

const log = (...args) => console.error("[visuals]", ...args);

const USAGE =
  "node render/visuals_preview.mjs --plan <plan.json> --assets <dir> " +
  "[--out <mp4>] [--from <кадр>] [--to <кадр>] [--stills 20,40,130 --stills-dir <dir>] " +
  "[--job-key <ключ>] [--concurrency <n>] [--gl <режим>] [--composition <id>]";

/** `--имя значение` → { имя: значение }. Флаг без значения — ошибка, не `true`. */
const parseArgs = (argv) => {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith("--")) throw new Error(`лишний аргумент: ${arg}\n${USAGE}`);
    const value = argv[i + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`у флага ${arg} нет значения\n${USAGE}`);
    }
    out[arg.slice(2)] = value;
    i++;
  }
  return out;
};

/** Самая свежая правка исходников рендера — по ней решаем, пересобирать ли бандл. */
const sourcesMtime = () => {
  let newest = 0;
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else newest = Math.max(newest, fs.statSync(p).mtimeMs);
    }
  };
  walk(path.join(HERE, "src"));
  newest = Math.max(newest, fs.statSync(path.join(HERE, "package.json")).mtimeMs);
  return newest;
};

/**
 * Убрать точку монтирования, не заходя внутрь неё.
 *
 * Только `unlink`/`rmdir`, никакого рекурсивного удаления: под ссылкой лежит
 * чужая папка ассетов, и инструмент предпросмотра не должен иметь способа её
 * стереть ни при какой ошибке. Настоящая непустая папка на месте ссылки
 * остаётся как есть — `rmdir` на ней просто падает.
 */
const removeLink = (link) => {
  let stat;
  try {
    stat = fs.lstatSync(link);
  } catch {
    return;
  }
  try {
    if (stat.isDirectory() && !stat.isSymbolicLink()) fs.rmdirSync(link);
    else fs.unlinkSync(link);
  } catch {
    try {
      // Соединение каталогов Windows часть версий Node снимает только так.
      fs.rmdirSync(link);
    } catch (err) {
      log(`не удалось убрать точку монтирования ${link}: ${err?.message || err}`);
    }
  }
};

const ensureBundle = async () => {
  const newest = sourcesMtime();
  const stamped = fs.existsSync(STAMP) ? Number(fs.readFileSync(STAMP, "utf8")) : -1;
  if (fs.existsSync(path.join(BUNDLE_DIR, "index.html")) && stamped >= newest) {
    return BUNDLE_DIR;
  }
  log("собираю бандл…");
  // Перед сносом бандла снимаем оставшиеся в нём ссылки на ассеты (прогон мог
  // упасть, не убрав за собой): удалять рекурсивно можно только сам бандл.
  if (fs.existsSync(MEDIA_DIR)) {
    for (const name of fs.readdirSync(MEDIA_DIR)) removeLink(path.join(MEDIA_DIR, name));
  }
  fs.rmSync(BUNDLE_DIR, { recursive: true, force: true });
  const started = Date.now();
  await bundle({
    entryPoint: ENTRY,
    outDir: BUNDLE_DIR,
    onProgress: () => {},
    webpackOverride: (c) => c,
  });
  fs.mkdirSync(path.dirname(STAMP), { recursive: true });
  fs.writeFileSync(STAMP, String(newest));
  log(`бандл готов за ${((Date.now() - started) / 1000).toFixed(1)}с`);
  return BUNDLE_DIR;
};

/** Ассеты монтируются в бандл ссылкой: копировать видео на каждый прогон незачем. */
const mountAssets = (assetsDir, jobKey) => {
  fs.mkdirSync(MEDIA_DIR, { recursive: true });
  const link = path.join(MEDIA_DIR, jobKey);
  removeLink(link);
  // На Windows — соединение каталогов: символической ссылке нужны права
  // администратора (см. `mountAssets` в `render.mjs`).
  fs.symlinkSync(path.resolve(assetsDir), link, process.platform === "win32" ? "junction" : "dir");
  return `media/${jobKey}`;
};

const withPrefix = (prefix, value) => (value ? `${prefix}/${value}` : value);

const fail = (message) => {
  throw new Error(message);
};

const frameNumber = (value, name) => {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0) fail(`--${name}: нужен номер кадра, а дано ${value}`);
  return n;
};

const main = async () => {
  const args = parseArgs(process.argv.slice(2));
  if (!args.plan) fail(`нет --plan\n${USAGE}`);
  if (!args.assets) fail(`нет --assets\n${USAGE}`);
  if (!args.out && !args.stills) fail(`нужен --out или --stills\n${USAGE}`);
  if (args.stills && !args["stills-dir"]) fail(`к --stills нужен --stills-dir\n${USAGE}`);
  if (!fs.existsSync(args.assets)) fail(`папки ассетов нет: ${args.assets}`);

  const plan = JSON.parse(fs.readFileSync(args.plan, "utf8"));
  const jobKey = args["job-key"] || `visuals-${process.pid}`;
  // Ключ становится именем папки внутри бандла: ни разделителей, ни «..».
  if (!/^[\w-][\w.-]*$/.test(jobKey)) fail(`--job-key: только буквы, цифры, . _ -: ${jobKey}`);
  const compositionId = args.composition || "ReelVisuals";
  const gl = args.gl || "swangle";

  const serveUrl = await ensureBundle();
  await ensureBrowser();
  const prefix = mountAssets(args.assets, jobKey);

  try {
    // Те же поля, что дописывает `render.mjs`, и ничего сверх: графика обязана
    // найти свои картинки по пути видео, как найдёт их в боевом рендере.
    const inputProps = {
      ...plan,
      video: withPrefix(prefix, plan.video),
      audio: withPrefix(prefix, plan.audio),
      fontUrl: withPrefix(prefix, plan.fontUrl),
      accentFontUrl: withPrefix(prefix, plan.accentFontUrl),
      emojiFontUrl: withPrefix(prefix, plan.emojiFontUrl),
      broll: (plan.broll ?? []).map((b) => ({ ...b, asset: withPrefix(prefix, b.asset) })),
      blocks: (plan.blocks ?? []).map((b) =>
        b.behind ? { ...b, behind: { ...b.behind, dir: withPrefix(prefix, b.behind.dir) } } : b,
      ),
    };

    const composition = await selectComposition({ serveUrl, id: compositionId, inputProps });
    const lastFrame = composition.durationInFrames - 1;
    const timeoutInMilliseconds = Number(args["timeout-ms"] || 120_000);

    if (args.stills) {
      const frames = args.stills
        .split(",")
        .filter((part) => part.trim() !== "")
        .map((part) => frameNumber(part.trim(), "stills"));
      const dir = path.resolve(args["stills-dir"]);
      fs.mkdirSync(dir, { recursive: true });
      // Один браузер на все кадры: запуск Chromium дольше самого снимка.
      const browser = await openBrowser("chrome", { chromiumOptions: { gl } });
      const started = Date.now();
      try {
        for (const frame of frames) {
          if (frame > lastFrame) throw new Error(`кадр ${frame} за концом ролика (${lastFrame})`);
          const output = path.join(dir, `frame-${String(frame).padStart(5, "0")}.png`);
          await renderStill({
            composition,
            serveUrl,
            output,
            frame,
            inputProps,
            imageFormat: "png",
            puppeteerInstance: browser,
            chromiumOptions: { gl },
            timeoutInMilliseconds,
          });
          process.stdout.write(output + "\n");
        }
      } finally {
        await browser.close({ silent: true });
      }
      log(`кадров ${frames.length} за ${((Date.now() - started) / 1000).toFixed(1)}с`);
    }

    if (args.out) {
      const from = args.from === undefined ? 0 : frameNumber(args.from, "from");
      const to = args.to === undefined ? lastFrame : frameNumber(args.to, "to");
      if (from > to || to > lastFrame) {
        throw new Error(`отрезок ${from}..${to} не лежит в ролике 0..${lastFrame}`);
      }
      const out = path.resolve(args.out);
      fs.mkdirSync(path.dirname(out), { recursive: true });
      const started = Date.now();
      let lastLogged = 0;
      await renderMedia({
        composition,
        serveUrl,
        codec: "h264",
        outputLocation: out,
        inputProps,
        frameRange: [from, to],
        crf: Number(args.crf || 18),
        x264Preset: "medium",
        pixelFormat: "yuv420p",
        concurrency: args.concurrency ? Number(args.concurrency) : null,
        chromiumOptions: { gl },
        timeoutInMilliseconds,
        onProgress: ({ progress }) => {
          const pct = Math.round(progress * 100);
          if (pct >= lastLogged + 10) {
            lastLogged = pct;
            log(`progress ${pct}`);
          }
        },
      });
      const seconds = (Date.now() - started) / 1000;
      const video = (to - from + 1) / composition.fps;
      log(
        `готово за ${seconds.toFixed(1)}с (${video.toFixed(1)}с видео, ` +
          `${(seconds / video).toFixed(2)}x, кадры ${from}..${to})`,
      );
      process.stdout.write(out + "\n");
    }
  } finally {
    removeLink(path.join(MEDIA_DIR, jobKey));
  }
};

main().catch((err) => {
  log("ОШИБКА:", err?.stack || err?.message || String(err));
  process.exit(1);
});
