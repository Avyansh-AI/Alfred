/*
 * tools/browser.mjs - find a browser and launch it, for the browser checks.
 *
 * Order of preference:
 *   1. --exe /path/to/chrome
 *   2. a Chrome/Chromium already installed on this machine (puppeteer channel)
 *   3. @sparticuz/chromium from npm - it ships a chromium build *and* the shared
 *      libraries it needs inside the package, which is how this gets a real
 *      browser inside a bare container that has no CDN access.
 *
 * Set ALFRED_BROWSER_DEPS to a colon-separated list of folders where npm packages
 * like puppeteer-core / @sparticuz/chromium live (default: /tmp/browser-check).
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const HERE = path.dirname(new URL(import.meta.url).pathname);
export const ROOT = path.resolve(HERE, '..');
const DEP_HINTS = (process.env.ALFRED_BROWSER_DEPS || '/tmp/browser-check')
  .split(':').filter(Boolean);

export function resolveAny(name){
  for (const root of [ROOT, process.cwd(), ...DEP_HINTS]){
    try {
      const req = createRequire(path.join(root, 'package.json'));
      return pathToFileURL(req.resolve(name)).href;
    } catch (_) { /* try the next root */ }
  }
  return null;
}

export function noPuppeteerMessage(){
  console.error('browser-check: puppeteer-core is not installed.');
  console.error('  install it somewhere and point ALFRED_BROWSER_DEPS at that folder:');
  console.error('    mkdir -p /tmp/browser-check && cd /tmp/browser-check');
  console.error('    npm i puppeteer-core @sparticuz/chromium');
  console.error('    ALFRED_BROWSER_DEPS=/tmp/browser-check node tools/browser-check.mjs');
}

export async function launchBrowser({ exe = null, channel = null, args = [] } = {}){
  const pptrUrl = resolveAny('puppeteer-core');
  if (!pptrUrl) return {error: 'no-puppeteer'};
  const puppeteer = (await import(pptrUrl)).default;

  let executablePath = exe || undefined;
  let extraArgs = [];
  const chromiumUrl = resolveAny('@sparticuz/chromium');
  if (!exe && chromiumUrl){
    const chromium = (await import(chromiumUrl)).default;
    executablePath = await chromium.executablePath();
    extraArgs = chromium.args.filter(a => a !== "--headless='shell'");
    // the package ships the shared libraries chromium needs; it only puts them on
    // the loader path automatically when it detects Amazon Linux
    const libs = '/tmp/al2023/lib';
    if (!fs.existsSync(libs)){
      console.log('browser-check: extracting chromium shared libraries...');
      const { brotliDecompressSync } = await import('node:zlib');
      const { execFileSync } = await import('node:child_process');
      const pkgDir = path.dirname(chromiumUrl.replace('file://', ''));
      const binDir = path.join(pkgDir, '..', 'bin');
      for (const [file, dest] of [['al2023.tar.br', '/tmp/al2023'], ['fonts.tar.br', '/tmp/fonts']]){
        const tar = '/tmp/' + path.basename(file, '.br');
        try {
          fs.writeFileSync(tar, brotliDecompressSync(fs.readFileSync(path.join(binDir, file))));
          fs.mkdirSync(dest, {recursive: true});
          execFileSync('tar', ['xf', tar, '-C', dest]);
        } catch (err){
          console.warn('  could not stage ' + file + ': ' + err.message);
        }
      }
    }
    if (fs.existsSync(libs)){
      process.env.LD_LIBRARY_PATH = [libs, process.env.LD_LIBRARY_PATH].filter(Boolean).join(':');
      process.env.FONTCONFIG_PATH = '/tmp/fonts';
      process.env.HOME = process.env.HOME || '/tmp';
    }
  }

  // A launch can fail transiently - most often "Code: null" when another chromium has
  // only just exited and its profile is still being torn down. One retry turns that into
  // a non-event instead of a red run.
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt++){
    try {
      const browser = await puppeteer.launch({
        executablePath,
        channel: executablePath ? undefined : (channel || undefined),
        headless: true,
        args: [...extraArgs, ...args, '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']
      });
      return {browser, puppeteer};
    } catch (err){
      lastError = err;
      if (attempt === 0) await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }
  return {error: 'launch-failed', message: String(lastError.message).split('\n')[0]};
}
