#!/usr/bin/env node
/**
 * CLI рендера: JSON на stdin → путь к mp4 на stdout.
 *
 * Node только рендерит. Никакого HTTP-сервиса между Python и Node — это лишняя
 * точка отказа (§1). Всё состояние приходит в одном JSON.
 *
 * Формат stdin:
 * {
 *   "plan":       { ...RenderPlan... },  // пути внутри — имена файлов в assetsDir
 *   "assetsDir":  "/abs/job/dir",        // монтируется в бандл как media/<key>
 *   "out":        "/abs/out.mp4",
 *   "concurrency": 2,
 *   "crf": 18,
 *   "maxVideoKbps": 1900,                // необязательно: потолок битрейта видео
 *   "audioKbps": 128,                    // необязательно: битрейт звука
 *   "jobKey": "job-123"
 * }
 *
 * Служебные режимы:
 *   node render.mjs --ensure-browser   скачать Chromium заранее (на старте воркера)
 *   node render.mjs --bundle           только собрать бандл
 */

import { bundle } from "@remotion/bundler";
import {
  ensureBrowser,
  openBrowser,
  renderFrames,
  renderMedia,
  selectComposition,
} from "@remotion/renderer";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENTRY = path.join(HERE, "src", "index.ts");
const BUNDLE_DIR = path.join(HERE, ".cache", "bundle");
// staticFile() резолвится в /public/<path>, поэтому монтируем именно в public.
const MEDIA_DIR = path.join(BUNDLE_DIR, "public", "media");
const STAMP = path.join(HERE, ".cache", "bundle.stamp");

const log = (...args) => console.error("[render]", ...args);

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

const ensureBundle = async () => {
  const newest = sourcesMtime();
  const stamped = fs.existsSync(STAMP) ? Number(fs.readFileSync(STAMP, "utf8")) : -1;
  if (fs.existsSync(path.join(BUNDLE_DIR, "index.html")) && stamped >= newest) {
    return BUNDLE_DIR;
  }
  log("собираю бандл…");
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

/** Ассеты джоба монтируются в бандл симлинком: копировать гигабайты незачем. */
const mountAssets = (assetsDir, jobKey) => {
  fs.mkdirSync(MEDIA_DIR, { recursive: true });
  const link = path.join(MEDIA_DIR, jobKey);
  try {
    if (fs.existsSync(link) || fs.lstatSync(link)) fs.rmSync(link, { recursive: true, force: true });
  } catch {
    /* ссылки ещё нет */
  }
  // На Windows — соединение каталогов (junction), а не символическая ссылка:
  // ссылке нужны права администратора или режим разработчика, и без них рендер
  // падал на EPERM, не начавшись. Соединению права не нужны, а удаляется оно так
  // же, как ссылка, — содержимое папки джоба при этом не трогается (проверено).
  // На Linux тип игнорируется, там по-прежнему обычная ссылка.
  fs.symlinkSync(path.resolve(assetsDir), link, process.platform === "win32" ? "junction" : "dir");
  return `media/${jobKey}`;
};

const unmountAssets = (jobKey) => {
  const link = path.join(MEDIA_DIR, jobKey);
  try {
    fs.rmSync(link, { recursive: true, force: true });
  } catch {
    /* уже убрано */
  }
};

const withPrefix = (prefix, value) => (value ? `${prefix}/${value}` : value);

const readStdin = () =>
  new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });

/**
 * Кадры для быстрого рендера (`src/pipeline/fastrender.py`): не ролик, а
 * отдельные картинки — только те кадры и слои, которые не умеет ffmpeg.
 *
 * Проход — `{layers, frames, dir}`: `overlay` снимается в PNG с прозрачностью
 * и без видео, `under` — целым кадром в JPEG. Браузер один на все проходы:
 * запуск Chromium стоит секунды, а проходов обычно два. Файлы называются
 * `element-<номер кадра>.<ext>` — номер кадра ролика, а не порядковый.
 */
const renderPasses = async (req, serveUrl, baseProps, prefix) => {
  const browser = await openBrowser("chrome", {
    chromiumOptions: { gl: req.gl ?? "swangle" },
  });
  try {
    for (const pass of req.passes) {
      if (!Array.isArray(pass.frames) || pass.frames.length === 0) continue;
      const overlay = pass.layers === "overlay";
      const inputProps = {
        ...baseProps,
        layers: pass.layers,
        browserBlocks: req.browserBlocks ?? [],
        // Графика ищет свои картинки по префиксу папки задачи; без видео в
        // плане вывести его не из чего — отдаём явно.
        assetsPrefix: prefix,
        ...(overlay
          ? { video: null, audio: null, preset: { ...baseProps.preset, background: "transparent" } }
          : {}),
      };
      const composition = await selectComposition({
        serveUrl,
        id: "Reel",
        inputProps,
        puppeteerInstance: browser,
      });
      fs.rmSync(pass.dir, { recursive: true, force: true });
      fs.mkdirSync(pass.dir, { recursive: true });
      const started = Date.now();
      let done = 0;
      let lastLogged = 0;
      await renderFrames({
        composition,
        serveUrl,
        inputProps,
        outputDir: pass.dir,
        frames: pass.frames,
        imageFormat: overlay ? "png" : "jpeg",
        ...(overlay ? {} : { jpegQuality: 92 }),
        concurrency: req.concurrency ?? null,
        puppeteerInstance: browser,
        chromiumOptions: { gl: req.gl ?? "swangle" },
        timeoutInMilliseconds: req.timeoutMs ?? 120_000,
        onStart: () => {},
        // Тот же контракт с Python, что у ролика: `[render] progress N`, но
        // доля — от всех кадров всех проходов (`req.totalFrames`).
        // `[render] frame <проход> <номер>` — кадр записан на диск: Python
        // берёт его в ролик, не дожидаясь конца прохода.
        onFrameUpdate: (_rendered, frameIndex) => {
          log(`frame ${pass.layers} ${frameIndex}`);
          done += 1;
          const pct = Math.round(((req.doneBefore ?? 0) + done) / (req.totalFrames || 1) * 100);
          if (pct >= lastLogged + 2) {
            lastLogged = pct;
            log(`progress ${pct}`);
          }
        },
      });
      req.doneBefore = (req.doneBefore ?? 0) + pass.frames.length;
      log(`проход ${pass.layers}: ${pass.frames.length} кадров за ${((Date.now() - started) / 1000).toFixed(1)}с`);
    }
  } finally {
    await browser.close({ silent: true });
  }
  process.stdout.write("ok\n");
};

const main = async () => {
  const args = process.argv.slice(2);

  if (args.includes("--ensure-browser")) {
    // Вызывается один раз при старте воркера, а не на каждый рендер (§5 M5 ⚠).
    await ensureBrowser();
    log("браузер готов");
    return;
  }
  if (args.includes("--bundle")) {
    await ensureBundle();
    return;
  }

  const raw = await readStdin();
  if (!raw.trim()) throw new Error("на stdin пусто: ожидался JSON с планом рендера");
  const req = JSON.parse(raw);
  const plan = req.plan;
  if (!plan) throw new Error("в запросе нет поля plan");
  const out = req.out;
  if (!out) throw new Error("в запросе нет поля out");
  const jobKey = req.jobKey || `job-${process.pid}-${Math.floor(Math.random() * 1e6)}`;

  const serveUrl = await ensureBundle();
  await ensureBrowser();

  let prefix = "";
  if (req.assetsDir) prefix = mountAssets(req.assetsDir, jobKey);

  try {
    const inputProps = {
      ...plan,
      video: withPrefix(prefix, plan.video),
      audio: withPrefix(prefix, plan.audio),
      fontUrl: withPrefix(prefix, plan.fontUrl),
      accentFontUrl: withPrefix(prefix, plan.accentFontUrl),
      emojiFontUrl: withPrefix(prefix, plan.emojiFontUrl),
      broll: (plan.broll ?? []).map((b) => ({ ...b, asset: withPrefix(prefix, b.asset) })),
      // Папка с масками блока, уходящего за голову, лежит там же, где видео.
      blocks: (plan.blocks ?? []).map((b) =>
        b.behind ? { ...b, behind: { ...b.behind, dir: withPrefix(prefix, b.behind.dir) } } : b,
      ),
    };

    if (Array.isArray(req.passes)) {
      await renderPasses(req, serveUrl, inputProps, prefix);
      return;
    }

    const composition = await selectComposition({
      serveUrl,
      id: "Reel",
      inputProps,
    });

    fs.mkdirSync(path.dirname(out), { recursive: true });
    const started = Date.now();
    let lastLogged = 0;

    await renderMedia({
      composition,
      serveUrl,
      codec: "h264",
      outputLocation: out,
      inputProps,
      crf: req.crf ?? 18,
      x264Preset: req.x264Preset ?? "medium",
      pixelFormat: "yuv420p",
      // Потолок битрейта по длине ролика (`src/pipeline/fit.py`): облачный Bot
      // API берёт от бота файл до 50 МБ, а crf за размером не следит. Качество
      // по-прежнему задаёт crf — потолок срабатывает только там, где без него
      // файл не пролез бы в чат. Буфер в две секунды потока.
      ...(req.maxVideoKbps
        ? {
            encodingMaxRate: `${req.maxVideoKbps}k`,
            encodingBufferSize: `${req.maxVideoKbps * 2}k`,
          }
        : {}),
      ...(req.audioKbps ? { audioBitrate: `${req.audioKbps}k` } : {}),
      concurrency: req.concurrency ?? null,
      // Режим GL — число настройки (RENDER_GL), а не константа: на сервере без
      // GPU программная эмуляция WebGL стоит времени на каждом кадре, а слоям из
      // HTML и CSS WebGL не нужен. Не задан — прежний "swangle".
      chromiumOptions: { gl: req.gl ?? "swangle" },
      // Дефолтные 30 с на кадр — это не «кадр завис», а «машина занята». Замерено
      // на этой: при параллельном прогоне тестов рендер упал на кадре 221 с
      // «Timeout (30000ms) exceeded», не дождавшись куска ОСНОВНОГО видео с
      // собственного прокси-сервера. М4 делает это вероятнее: видео-вставка —
      // второй поток, который тянется оттуда же.
      //
      // Верхнюю границу держит вызывающий Python (таймаут процесса), поэтому
      // здесь порог нужен только чтобы отличить зависший кадр от медленного.
      // Две минуты на кадр — это заведомо зависший.
      timeoutInMilliseconds: req.timeoutMs ?? 120_000,
      // Строка `[render] progress N` — контракт с Python (`render._PROGRESS`):
      // по ней двигается полоса прогресса в чате и приложении. Шаг 2 %: рендер
      // бывает самым долгим этапом, и без этого полоса минутами стоит на месте.
      onProgress: ({ progress }) => {
        const pct = Math.round(progress * 100);
        if (pct >= lastLogged + 2) {
          lastLogged = pct;
          log(`progress ${pct}`);
        }
      },
    });

    const seconds = (Date.now() - started) / 1000;
    log(
      `готово за ${seconds.toFixed(1)}с (${(composition.durationInFrames / composition.fps).toFixed(1)}с видео, ` +
        `${(seconds / (composition.durationInFrames / composition.fps)).toFixed(2)}x)`,
    );
    process.stdout.write(out + "\n");
  } finally {
    if (prefix) unmountAssets(jobKey);
  }
};

main().catch((err) => {
  log("ОШИБКА:", err?.stack || err?.message || String(err));
  process.exit(1);
});
